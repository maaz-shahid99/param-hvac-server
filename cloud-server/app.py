"""
HVAC Cloud Server — the multi-tenant alerting product.

Runs on AWS alongside the discovery server, backed by RDS Postgres (or SQLite
for local dev). Responsibilities:

  - Ingest readings from gateways (X-API-Key -> tenant) and store them.
  - Evaluate every reading against per-tenant thresholds (see thresholds.py)
    and fire SES/SNS alerts with hysteresis + cooldown.
  - Watch for sensors that stop reporting (stale alerts).
  - Serve the app: JWT auth, topology sync, thresholds CRUD, current temps,
    alert list/ack.

Run:
    pip install -r requirements.txt
    uvicorn app:app --host 0.0.0.0 --port 8002
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import config
import discovery_routes
from auth import (
    Principal,
    SupportPrincipal,
    current_principal,
    generate_api_key,
    generate_org_code,
    generate_otp,
    get_db,
    hash_otp,
    hash_password,
    issue_token,
    require_admin,
    support_principal,
    tenant_from_api_key,
    verify_password,
)
from db import (
    Alert,
    ApiKey,
    CommissionedDevice,
    CrashReport,
    EnvReading,
    FirmwareRelease,
    FleetStatus,
    MeshNode,
    OtaState,
    PasswordReset,
    Reading,
    SensorMap,
    SessionLocal,
    SingletonLease,
    SupportAudit,
    Tenant,
    Threshold,
    Topology,
    User,
    init_db,
    new_id,
    now,
)
from notifications import notify_email
from thresholds import _clear_alert, evaluate_reading


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

# Ingest safety limits — reject/normalize untrusted gateway input so a parse
# glitch or rogue device can't poison the DB or false-trigger alerts.
MAX_PROBES = 16                       # a DS18B20 bus realistically carries <= ~10
MAX_DATA_LEN = 512                    # cap the raw CSV; longer => garbage/abuse
TEMP_MIN_C, TEMP_MAX_C = -55.0, 125.0  # DS18B20 operating range
_ROM_HEX = set("0123456789abcdef")


def clean_temp(c: float | None) -> float | None:
    """Out-of-range or non-finite readings are treated as 'err' (None) so absurd
    values never get stored or evaluated against thresholds."""
    if c is None:
        return None
    try:
        c = float(c)
    except (TypeError, ValueError):
        return None
    if c != c or c in (float("inf"), float("-inf")):   # NaN / inf
        return None
    return c if TEMP_MIN_C <= c <= TEMP_MAX_C else None


def clean_rom(rom: str, idx: int) -> str:
    """Normalize a probe ROM: keep a synth 'idxN', else strip to hex and cap
    length. Anything non-hex/empty falls back to the positional 'idxN'."""
    rom = (rom or "").strip().lower()
    if rom.startswith("idx"):
        return rom[:8]
    rom = "".join(ch for ch in rom if ch in _ROM_HEX)
    return rom[:16] if rom else f"idx{idx}"


def parse_probe_csv(data: str) -> list[dict]:
    """Parse the gateway's probe CSV into ROM-tagged readings.

    ROM form (firmware >= probe-id):   't=<rom>:23.1,<rom>:err'
    Legacy form (position only):       't=23.1,24.0,err'  -> synth roms idx0,idx1

    -> [{"rom": "28ff..", "c": 23.1}, {"rom": "idx1", "c": None}, ...]
    The synthesized roms keep the whole pipeline working with un-upgraded SEDs;
    once the SED reports real ROMs the mapping becomes plug/unplug-stable.
    Input is untrusted: the payload is length-capped, probe count is capped, ROMs
    are sanitized, and temps outside the DS18B20 range are dropped to 'err'.
    """
    data = (data or "")[:MAX_DATA_LEN]
    if "=" in data:
        data = data.split("=", 1)[1]
    out: list[dict] = []
    idx = 0
    for tok in data.split(","):
        if len(out) >= MAX_PROBES:
            break
        tok = tok.strip()
        if not tok:
            continue
        rom, val = "", tok
        if ":" in tok:
            rom, val = tok.split(":", 1)
            val = val.strip()
        c: float | None = None
        if val.lower() != "err":
            try:
                c = float(val)
            except ValueError:
                c = None
        out.append({"rom": clean_rom(rom, idx), "c": clean_temp(c)})
        idx += 1
    return out


def hottest(probes: list[dict]) -> float:
    vals = [p["c"] for p in probes if isinstance(p.get("c"), (int, float))]
    return max(vals) if vals else 0.0


def valid_eui(eui: str) -> bool:
    """A device EUI is exactly 16 lower-hex chars. Guards against a gateway
    parse-glitch (e.g. a mangled sensor_id that swallowed a log line) creating
    phantom 'devices' in the roster / readings."""
    return len(eui) == 16 and all(c in "0123456789abcdef" for c in eui)


def recipients_for(db: Session, tenant_id: str) -> tuple[list[str], list[str]]:
    """(emails, phones) for a tenant's alerts: ACTIVE members the admin has
    opted in, plus any extra external addresses on the tenant. Pending/rejected
    members and opted-out members are never notified."""
    members = db.scalars(
        select(User).where(User.tenant_id == tenant_id, User.status == "active")
    ).all()
    emails = [m.email for m in members if m.email_enabled and m.email]
    phones = [m.phone for m in members if m.sms_enabled and m.phone]

    t = db.get(Tenant, tenant_id)
    if t:
        emails += [e.strip() for e in (t.alert_emails or "").split(",") if e.strip()]
        phones += [p.strip() for p in (t.alert_phones or "").split(",") if p.strip()]

    # de-dupe, preserve order
    return list(dict.fromkeys(emails)), list(dict.fromkeys(phones))


def rebuild_sensor_map(db: Session, tenant_id: str, topo: dict) -> int:
    """Flatten the app's Rack->Unit->Port document into sensor_map rows so the
    ingest hot path is one indexed lookup. Rebuilt wholesale on each save."""
    db.execute(delete(SensorMap).where(SensorMap.tenant_id == tenant_id))
    count = 0
    seen: set[tuple[str, str]] = set()   # (eui, probe_rom) — one physical probe per row
    for rack in topo.get("racks", []):
        r_name = rack.get("name", "")
        for unit in rack.get("units", []):
            u_name = unit.get("name", "")
            for port in unit.get("ports", []):
                eui = (port.get("assignedEui") or "").strip().lower()
                if not eui:
                    continue
                probe_rom = (port.get("assignedProbeRom") or "").strip().lower()
                if (eui, probe_rom) in seen:
                    continue   # defensive: the app enforces one place per probe
                seen.add((eui, probe_rom))
                slot = "B" if port.get("type") == "exhaust" else "A"
                label = " / ".join(p for p in (r_name, u_name, port.get("label", "")) if p)
                db.add(SensorMap(
                    id=new_id(), tenant_id=tenant_id, eui=eui, probe_rom=probe_rom,
                    box=int(port.get("box", 0) or 0), slot=slot, label=label,
                    rack_id=str(rack.get("id", "")), unit_id=str(unit.get("id", "")),
                    port_id=str(port.get("id", "")),
                ))
                count += 1
    db.commit()
    return count


# --------------------------------------------------------------------------- #
# Stale-sensor watchdog                                                        #
# --------------------------------------------------------------------------- #

# A unique id for THIS process, used to claim the watchdog leader lease.
_INSTANCE_ID = uuid.uuid4().hex


def _acquire_lease(name: str, ttl: float) -> bool:
    """Try to hold/renew a named leader lease. Returns True iff this process is
    the current holder. Safe to call from every worker each tick: only one wins.
    The lease is taken for `ttl` seconds (> the tick interval) so the holder
    keeps it across ticks and a dead holder is replaced after it lapses."""
    t = now()
    with SessionLocal() as db:
        lease = db.get(SingletonLease, name)
        if lease is None:
            try:
                db.add(SingletonLease(name=name, holder=_INSTANCE_ID, expires_at=t + ttl))
                db.commit()
                return True
            except IntegrityError:        # another worker inserted first
                db.rollback()
                lease = db.get(SingletonLease, name)
        if lease is None:
            return False
        if lease.holder == _INSTANCE_ID or lease.expires_at < t:
            lease.holder = _INSTANCE_ID
            lease.expires_at = t + ttl
            db.commit()
            return True
        return False


async def stale_watchdog() -> None:
    # Lease lives longer than a tick so the holder renews before it lapses; if
    # the holder dies, a follower takes over within ~one lease period.
    lease_ttl = config.WATCHDOG_INTERVAL_S * 3
    while True:
        await asyncio.sleep(config.WATCHDOG_INTERVAL_S)
        try:
            if _acquire_lease("stale_watchdog", lease_ttl):
                _scan_stale()
        except Exception as exc:  # noqa: BLE001
            print(f"[watchdog] error: {exc}")


def _mesh_label(db: Session, mn: MeshNode) -> str:
    """Friendly label for a mesh node (router/gateway) in offline/online alerts."""
    d = db.scalar(select(CommissionedDevice).where(
        CommissionedDevice.tenant_id == mn.tenant_id, CommissionedDevice.eui == mn.eui))
    if d and d.name:
        return d.name
    return f"{mn.kind or 'router'} {mn.eui}"


def _scan_stale() -> None:
    cutoff = now() - config.STALE_AFTER_S
    with SessionLocal() as db:
        from thresholds import _clear_alert, _open_alert  # local import avoids a cycle
        # Mapped sensors: offline if their latest reading is stale. (Recovery for
        # sensors fires in the readings-ingest path when a fresh reading arrives.)
        for sm in db.scalars(select(SensorMap)).all():
            last = db.scalar(
                select(Reading).where(Reading.tenant_id == sm.tenant_id, Reading.eui == sm.eui)
                .order_by(Reading.ts.desc()).limit(1)
            )
            if last and last.ts < cutoff:
                loc = sm.label or f"sensor {sm.eui}"
                _open_alert(db, sm.tenant_id, recipients_for(db, sm.tenant_id),
                            sm.eui, "stale", loc, 0.0, 0.0)
        # Routers + gateway: they send no readings, so liveness is the mesh-roster
        # last_seen. Open an OFFLINE alert when stale; clear it + send a BACK ONLINE
        # email when they reappear (a no-op when there's no open alert).
        for mn in db.scalars(select(MeshNode)).all():
            rec = recipients_for(db, mn.tenant_id)
            label = _mesh_label(db, mn)
            if mn.last_seen < cutoff:
                _open_alert(db, mn.tenant_id, rec, mn.eui, "stale", label, 0.0, 0.0)
            else:
                _clear_alert(db, mn.tenant_id, mn.eui, "stale", rec, label)


# --------------------------------------------------------------------------- #
# App                                                                          #
# --------------------------------------------------------------------------- #

def _lan_ip() -> str:
    """Best-effort primary LAN IP (no traffic actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def _mdns_start(app: FastAPI) -> None:
    """Advertise the appliance as <MDNS_NAME>.local over mDNS so clients connect by
    name. Optional + fails open: a missing `zeroconf` or any error just logs and
    skips (you can always connect by IP)."""
    if not config.MDNS_ENABLED:
        return
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except Exception:
        print("[mdns] zeroconf not installed — skipping (connect by IP)")
        return
    try:
        ip = _lan_ip()
        info = ServiceInfo(
            "_http._tcp.local.",
            f"{config.MDNS_NAME}._http._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=config.PORT,
            properties={"path": "/", "role": "hvac-appliance"},
            server=f"{config.MDNS_NAME}.local.",
        )
        zc = Zeroconf()
        await asyncio.to_thread(zc.register_service, info)
        app.state.zeroconf = zc
        app.state.zeroconf_info = info
        print(f"[mdns] advertising http://{config.MDNS_NAME}.local:{config.PORT} ({ip})")
    except Exception as e:  # noqa: BLE001 — discovery is best-effort
        print(f"[mdns] registration failed (connect by IP): {e}")


def _mdns_stop(app: FastAPI) -> None:
    zc = getattr(app.state, "zeroconf", None)
    info = getattr(app.state, "zeroconf_info", None)
    if zc is None:
        return
    try:
        if info is not None:
            zc.unregister_service(info)
        zc.close()
    except Exception:  # noqa: BLE001
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.validate_startup()   # fail fast on insecure onprem/prod config (no-op in dev)
    init_db()
    discovery_routes.discovery_init_db()
    await _mdns_start(app)
    task = asyncio.create_task(stale_watchdog())
    discovery_task = asyncio.create_task(discovery_routes.discovery_watchdog())
    try:
        yield
    finally:
        task.cancel()
        discovery_task.cancel()
        _mdns_stop(app)


app = FastAPI(title="HVAC Cloud Server", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"], allow_headers=["*"],
)
# Discovery/rendezvous service, merged onto this same port at /discovery — see
# discovery_routes.py. Firmware derives "<cloud_url>/discovery" automatically
# (Bridge.ino's deriveDiscoveryUrl()) instead of needing a separate host:port.
app.include_router(discovery_routes.router, prefix="/discovery", tags=["discovery"])

# ...and ALSO at the root, for gateways provisioned before the merge. Their
# firmware appends "/register/sensor" / "/discover" to the bare URL it was given,
# which is what the standalone :8000 service served — so without this a deployed
# fleet 404s on every heartbeat and cannot be fixed without reflashing. Registered
# here, ahead of the SPA catch-all at the bottom of this file, so these win.
# Disable with DISCOVERY_LEGACY_ROOT=0 once no such firmware remains in the field.
if config.DISCOVERY_LEGACY_ROOT:
    app.include_router(discovery_routes.router, tags=["discovery (legacy root)"])


# ---- per-IP rate limit for auth endpoints ---------------------------------- #
# In-memory sliding window; mitigates password/OTP brute-force and registration
# spam. Per-process, so behind N workers the effective limit is N*max — use a
# shared store (Redis) if you need a hard global limit at scale.
_rl_hits: dict[str, deque] = defaultdict(deque)


def rate_limit(request: Request) -> None:
    # Honour X-Forwarded-For (set by the Nginx/ALB proxy) so we limit the real
    # client, not the proxy.
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")
    key = f"{request.url.path}:{ip}"
    win, now_t = config.AUTH_RATE_WINDOW_S, time.time()
    dq = _rl_hits[key]
    while dq and dq[0] < now_t - win:
        dq.popleft()
    if len(dq) >= config.AUTH_RATE_MAX:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests; slow down.")
    dq.append(now_t)


# ---- auth ------------------------------------------------------------------ #

class RegisterBody(BaseModel):
    bootstrap_token: str
    tenant_name: str
    name: str = ""
    email: str
    phone: str = ""
    password: str


class JoinBody(BaseModel):
    org_code: str
    name: str = ""
    email: str
    phone: str = ""
    password: str


class LoginBody(BaseModel):
    email: str
    password: str


def _auth_payload(db: Session, user: User) -> dict:
    tenant = db.get(Tenant, user.tenant_id)
    return {"token": issue_token(user), "tenant_id": user.tenant_id,
            "role": user.role, "status": user.status, "name": user.name,
            "email": user.email, "phone": user.phone,
            "org_code": tenant.org_code if tenant else ""}


@app.post("/v1/auth/register")
def register(body: RegisterBody, db: Session = Depends(get_db),
             _rl: None = Depends(rate_limit)):
    """Bootstrap a new org + its first admin (active). Guarded by BOOTSTRAP_TOKEN.
    Returns the org_code the admin shares so members can request to join."""
    if not config.BOOTSTRAP_TOKEN or body.bootstrap_token != config.BOOTSTRAP_TOKEN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bad bootstrap token")
    if db.scalar(select(User).where(User.email == body.email.lower())):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    # unique org code
    code = generate_org_code()
    while db.scalar(select(Tenant).where(Tenant.org_code == code)):
        code = generate_org_code()
    tenant = Tenant(id=new_id(), name=body.tenant_name, org_code=code)
    db.add(tenant)
    user = User(id=new_id(), tenant_id=tenant.id, name=body.name,
                email=body.email.lower(), phone=body.phone,
                password_hash=hash_password(body.password),
                role="admin", status="active", email_enabled=True, sms_enabled=bool(body.phone))
    db.add(user)
    db.commit()
    return _auth_payload(db, user)


@app.post("/v1/auth/join")
def join(body: JoinBody, background: BackgroundTasks, db: Session = Depends(get_db),
         _rl: None = Depends(rate_limit)):
    """Register as a MEMBER of an existing org (by org code). The member starts
    'pending' — an admin must approve before they receive notifications."""
    tenant = db.scalar(select(Tenant).where(Tenant.org_code == body.org_code.strip().upper()))
    if not tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown organization code")
    if db.scalar(select(User).where(User.email == body.email.lower())):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    # A new member gets NO notifications until an admin approves them AND opts
    # them in (admin controls who receives email / SMS).
    user = User(id=new_id(), tenant_id=tenant.id, name=body.name,
                email=body.email.lower(), phone=body.phone,
                password_hash=hash_password(body.password),
                role="member", status="pending",
                email_enabled=False, sms_enabled=False)
    db.add(user)
    db.commit()

    # Tell the admins somebody is waiting. Without this a request sat in the
    # database until an admin happened to open the Members page — there is no
    # badge and the alert bell only counts temperature alerts, so a Friday
    # request could go unnoticed all weekend while the applicant assumed the
    # system was broken. Queued in the background so a slow SMTP handshake
    # doesn't hold up the applicant's response.
    admins = _admin_emails(db, tenant.id)
    if admins:
        who = f"{body.name} ({user.email})" if body.name else user.email
        background.add_task(
            notify_email, admins,
            f"[{tenant.name}] {who} requested to join",
            f"{who} has asked to join {tenant.name} on HVAC Monitor.\n\n"
            f"They cannot sign in or receive alerts until an admin approves them.\n"
            f"Approve or reject them on the Members page of the dashboard.",
        )
    return _auth_payload(db, user)


@app.post("/v1/auth/login")
def login(body: LoginBody, db: Session = Depends(get_db),
          _rl: None = Depends(rate_limit)):
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    if user.status == "rejected":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your join request was declined")
    return _auth_payload(db, user)


@app.get("/v1/me")
def me(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    """The caller's own profile — lets a pending member poll for approval."""
    u = db.get(User, p.user_id)
    if not u:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    tenant = db.get(Tenant, u.tenant_id)
    return {"role": u.role, "status": u.status, "name": u.name,
            "email": u.email, "phone": u.phone,
            "org_code": tenant.org_code if tenant else "",
            "email_enabled": u.email_enabled, "sms_enabled": u.sms_enabled}


# ---- members (admin: approve join requests + control who gets notified) ---- #

def _member_dict(u: User) -> dict:
    return {"id": u.id, "name": u.name, "email": u.email, "phone": u.phone,
            "role": u.role, "status": u.status,
            "email_enabled": u.email_enabled, "sms_enabled": u.sms_enabled,
            "created_at": u.created_at}


@app.get("/v1/members")
def list_members(state: str = "all", p: Principal = Depends(current_principal),
                 db: Session = Depends(get_db)):
    """Roster for the caller's org (any signed-in member can VIEW it; approving
    and changing notification settings stay admin-only)."""
    q = select(User).where(User.tenant_id == p.tenant_id)
    if state in ("pending", "active", "rejected"):
        q = q.where(User.status == state)
    rows = db.scalars(q.order_by(User.created_at.desc())).all()
    return {"members": [_member_dict(u) for u in rows]}


def _get_member(db: Session, p: Principal, member_id: str) -> User:
    u = db.get(User, member_id)
    if not u or u.tenant_id != p.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Member not found")
    return u


def _admin_emails(db: Session, tenant_id: str) -> list[str]:
    """Emails of the ACTIVE admins for account notices (join requests, etc.).

    Deliberately ignores `email_enabled`: that flag governs temperature ALERTS.
    An admin who opted out of overheat emails still has to hear that somebody is
    waiting on them for access — otherwise a request sits unseen indefinitely,
    which is exactly what happened before this existed."""
    rows = db.scalars(
        select(User).where(User.tenant_id == tenant_id,
                           User.role == "admin", User.status == "active")
    ).all()
    return [u.email for u in rows if u.email]


def _active_admins(db: Session, tenant_id: str) -> int:
    """How many active admins the org has. An org with zero admins is stranded:
    nobody can approve join requests, edit thresholds, mint a gateway key or read
    crash reports — and there's no self-service way back. Every path that could
    remove an admin has to check this first."""
    return len(db.scalars(
        select(User).where(User.tenant_id == tenant_id,
                           User.role == "admin", User.status == "active")
    ).all())


@app.post("/v1/members/{member_id}/approve")
def approve_member(member_id: str, background: BackgroundTasks,
                   p: Principal = Depends(require_admin),
                   db: Session = Depends(get_db)):
    u = _get_member(db, p, member_id)
    was_pending = u.status == "pending"
    u.status = "active"
    db.commit()
    # Close the loop with the applicant, who is otherwise refreshing a "waiting
    # for approval" screen with no way to tell whether anyone has looked.
    if was_pending and u.email:
        t = db.get(Tenant, p.tenant_id)
        org = t.name if t else "your organization"
        background.add_task(
            notify_email, [u.email],
            f"You've been approved for {org}",
            f"An admin approved your request to join {org} on HVAC Monitor.\n\n"
            f"You can sign in now. An admin controls whether you receive email or "
            f"SMS alerts, so ask them to switch those on if you need them.",
        )
    return {"ok": True, "member": _member_dict(u)}


@app.post("/v1/members/{member_id}/reject")
def reject_member(member_id: str, background: BackgroundTasks,
                  p: Principal = Depends(require_admin),
                  db: Session = Depends(get_db)):
    u = _get_member(db, p, member_id)
    if u.id == p.user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't reject yourself")
    was_pending = u.status == "pending"
    u.status = "rejected"
    u.email_enabled = False
    u.sms_enabled = False
    db.commit()
    # Say so rather than leaving them on a screen that waits forever.
    if was_pending and u.email:
        t = db.get(Tenant, p.tenant_id)
        org = t.name if t else "the organization"
        background.add_task(
            notify_email, [u.email],
            f"Your request to join {org} was declined",
            f"An admin declined your request to join {org} on HVAC Monitor.\n\n"
            f"If you think this is a mistake, contact them directly.",
        )
    return {"ok": True, "member": _member_dict(u)}


class MemberNotifyBody(BaseModel):
    email_enabled: bool | None = None
    sms_enabled: bool | None = None
    role: str | None = None          # admin|member (promote/demote)


@app.put("/v1/members/{member_id}/notifications")
def set_member_notifications(member_id: str, body: MemberNotifyBody,
                             p: Principal = Depends(require_admin),
                             db: Session = Depends(get_db)):
    """Admin toggles who receives email / SMS (and optionally promotes a member
    to admin). Only active members can be opted in."""
    u = _get_member(db, p, member_id)
    if body.email_enabled is not None:
        u.email_enabled = body.email_enabled
    if body.sms_enabled is not None:
        if body.sms_enabled and not u.phone:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Member has no phone number")
        u.sms_enabled = body.sms_enabled
    if body.role in ("admin", "member"):
        # Refuse to demote the last admin — that would strand the org with nobody
        # able to administer it, including whoever is making this call.
        if (u.role == "admin" and body.role == "member"
                and u.status == "active" and _active_admins(db, p.tenant_id) <= 1):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "This is the only admin — promote another member to admin first.",
            )
        u.role = body.role
    db.commit()
    return {"ok": True, "member": _member_dict(u)}


@app.post("/v1/members/me/leave")
def leave_org(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    """Remove yourself from the organization. Deletes the account, so the org's
    roster and alert recipients no longer include you; rejoining later is the
    normal join-by-org-code flow.

    Blocked for the last remaining admin — see _active_admins()."""
    u = db.get(User, p.user_id)
    if not u:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if u.role == "admin" and u.status == "active" and _active_admins(db, u.tenant_id) <= 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "You are the only admin — promote another member to admin before leaving.",
        )
    email = u.email
    db.delete(u)
    db.execute(delete(PasswordReset).where(PasswordReset.email == email))
    db.commit()
    return {"ok": True}


# ---- password reset (email OTP) -------------------------------------------- #

class ForgotBody(BaseModel):
    email: str


class ResetBody(BaseModel):
    email: str
    otp: str
    new_password: str


@app.post("/v1/auth/forgot")
def forgot_password(body: ForgotBody, db: Session = Depends(get_db),
                    _rl: None = Depends(rate_limit)):
    """Email a 6-digit reset code. ALWAYS returns 200 and never reveals whether
    the email exists (anti-enumeration). Any previous code for this email is
    invalidated. Locally (no SES) the code is printed to the server log."""
    email = body.email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user:
        db.execute(delete(PasswordReset).where(PasswordReset.email == email))
        code = generate_otp()
        db.add(PasswordReset(id=new_id(), email=email, otp_hash=hash_otp(code),
                             expires_at=now() + config.OTP_TTL_S, attempts=0))
        db.commit()
        mins = int(config.OTP_TTL_S // 60)
        notify_email(
            [email], "HVAC Monitor password reset code",
            f"Your password reset code is: {code}\n\n"
            f"It expires in {mins} minutes. If you didn't request this, ignore this email.",
        )
    return {"ok": True}


@app.post("/v1/auth/reset")
def reset_password(body: ResetBody, db: Session = Depends(get_db),
                   _rl: None = Depends(rate_limit)):
    """Verify the emailed code and set a new password. Generic errors avoid
    leaking which of email/code was wrong; the code dies after OTP_MAX_ATTEMPTS."""
    if len(body.new_password) < config.MIN_PASSWORD_LEN:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Password must be at least {config.MIN_PASSWORD_LEN} characters",
        )
    email = body.email.strip().lower()
    pr = db.scalar(select(PasswordReset).where(PasswordReset.email == email))
    if not pr or pr.expires_at < now():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired code")
    if pr.attempts >= config.OTP_MAX_ATTEMPTS:
        db.execute(delete(PasswordReset).where(PasswordReset.email == email))
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Too many attempts; request a new code")
    if hash_otp(body.otp) != pr.otp_hash:
        pr.attempts += 1
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired code")
    user = db.scalar(select(User).where(User.email == email))
    if not user:                                   # code existed but user gone — treat as invalid
        db.execute(delete(PasswordReset).where(PasswordReset.email == email))
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired code")
    user.password_hash = hash_password(body.new_password)
    db.execute(delete(PasswordReset).where(PasswordReset.email == email))
    db.commit()
    return {"ok": True}


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


@app.post("/v1/auth/change-password")
def change_password(body: ChangePasswordBody, p: Principal = Depends(current_principal),
                    db: Session = Depends(get_db), _rl: None = Depends(rate_limit)):
    """Change your own password while signed in. Until now the only route was the
    emailed-OTP reset, which is useless if SMTP isn't configured — leaving no way
    to rotate a password that's been shared or exposed.

    The CURRENT password is required, so a stolen session token alone can't lock
    the owner out. Rate-limited like the other auth endpoints.

    NOTE: this does not invalidate tokens already issued. JWTs here are stateless
    and carry no revocation list, so any existing session stays valid until it
    expires (JWT_EXPIRE_HOURS). To force everyone off immediately, rotate
    JWT_SECRET — that invalidates every token, including your own.
    """
    user = db.get(User, p.user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Current password is incorrect")
    if len(body.new_password) < config.MIN_PASSWORD_LEN:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Password must be at least {config.MIN_PASSWORD_LEN} characters",
        )
    if body.new_password == body.current_password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "New password must be different")
    user.password_hash = hash_password(body.new_password)
    # Any outstanding emailed reset code for this account is now moot.
    db.execute(delete(PasswordReset).where(PasswordReset.email == user.email))
    db.commit()
    return {"ok": True}


# ---- API keys (gateway credential) ----------------------------------------- #

class ApiKeyBody(BaseModel):
    label: str = ""


@app.post("/v1/apikeys")
def create_api_key(body: ApiKeyBody, p: Principal = Depends(require_admin),
                   db: Session = Depends(get_db)):
    raw, key_hash = generate_api_key()
    db.add(ApiKey(id=new_id(), tenant_id=p.tenant_id, key_hash=key_hash, label=body.label))
    db.commit()
    # The raw key is returned ONCE; only its hash is stored.
    return {"api_key": raw, "label": body.label}


@app.get("/v1/apikeys")
def list_api_keys(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    # `id` is needed so a key can be revoked; the raw key is never recoverable
    # (only its hash is stored), so exposing the id leaks nothing.
    rows = db.scalars(select(ApiKey).where(ApiKey.tenant_id == p.tenant_id)).all()
    return {"keys": [{"id": k.id, "label": k.label, "created_at": k.created_at,
                      "last_used_at": k.last_used_at} for k in rows]}


@app.delete("/v1/apikeys/{key_id}")
def delete_api_key(key_id: str, p: Principal = Depends(require_admin),
                   db: Session = Depends(get_db)):
    """Revoke a key. There was no way to remove one, so a mis-clicked "Gateway
    API key" button left an extra key on the tenant forever — and a leaked key
    could never be withdrawn.

    Refuses to delete the LAST key that a gateway is actively using: that would
    cut the uplink, and the gateway holds the raw key in NVS, so it cannot be
    re-provisioned without physical BLE access."""
    k = db.get(ApiKey, key_id)
    if not k or k.tenant_id != p.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Key not found")
    recently_used = (k.last_used_at or 0) > now() - 900   # 15 min
    others = db.scalars(select(ApiKey).where(ApiKey.tenant_id == p.tenant_id,
                                             ApiKey.id != key_id)).all()
    if recently_used and not others:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This key is in active use by a gateway and is the only one left. "
            "Deleting it would stop the gateway reporting, and it cannot be "
            "re-provisioned without physical access to the device.",
        )
    db.delete(k)
    db.commit()
    return {"ok": True}


# ---- ingest (gateway -> cloud) --------------------------------------------- #

class IngestBody(BaseModel):
    sensor_id: str                                   # EUI-64 hex
    probes: list[float | None] = Field(default_factory=list)
    data: str | None = None                          # raw "t=<rom>:23.1,..." form
    ts: float | None = None


@app.post("/v1/readings")
def ingest(body: IngestBody, tenant_id: str = Depends(tenant_from_api_key),
           db: Session = Depends(get_db)):
    """Gateway posts a reading. Authenticated by X-API-Key -> tenant."""
    if body.data:
        probes = parse_probe_csv(body.data)                       # [{"rom","c"}]
    else:   # rare: a client that posts bare floats -> synth position roms
        probes = [{"rom": f"idx{i}", "c": clean_temp(v)}
                  for i, v in enumerate(body.probes[:MAX_PROBES])]
    eui = body.sensor_id.strip().lower()
    if not valid_eui(eui):
        # drop a malformed reading rather than create a phantom sensor (200 so the
        # gateway doesn't retry-spam).
        return {"ok": False, "error": "invalid sensor_id", "eui": eui}
    ts = body.ts or now()
    mx = hottest(probes)

    # box/slot on the Reading is best-effort metadata; the per-probe location now
    # comes from SensorMap at read/eval time. Use any mapped row for it.
    sm = db.scalar(select(SensorMap).where(SensorMap.tenant_id == tenant_id, SensorMap.eui == eui))
    db.add(Reading(
        tenant_id=tenant_id, ts=ts, eui=eui,
        box=(sm.box if sm else 0), slot=(sm.slot if sm else "A"),
        probes=json.dumps(probes), max_c=mx,
    ))
    db.commit()

    # A fresh reading clears any open "stale" alert for this sensor — and (if one
    # was open) emails a BACK ONLINE recovery notice to the same recipients.
    rec = recipients_for(db, tenant_id)
    loc = sm.label if (sm and sm.label) else f"sensor {eui}"
    _clear_alert(db, tenant_id, eui, "stale", rec, loc)
    evaluate_reading(db, tenant_id, eui, probes, mx, rec)
    return {"ok": True, "eui": eui, "max_c": mx}


# ---- mesh roster (routers) ------------------------------------------------- #

class MeshBody(BaseModel):
    nodes: list[dict] = []     # [{"eui": "...", "role": "G|R"}]
    routers: list[dict] = []   # legacy: [{"eui": "..."}] (treated as routers)
    # The active gateway's self-report (optional) — drives fleet health + OTA.
    fw_c3: int | None = None
    fw_c6: int | None = None
    heap_free: int | None = None
    role: str | None = None


@app.post("/v1/mesh")
def ingest_mesh(body: MeshBody, tenant_id: str = Depends(tenant_from_api_key),
                db: Session = Depends(get_db)):
    """Gateway posts the live C6 mesh roster (gateway + routers — these have no
    readings of their own). Upserts each as a MeshNode with a fresh last_seen +
    kind; the dashboard derives online/offline from the timestamp, and the
    'gateway' kind moves on failover. The optional self-report (fw/heap/role) is
    upserted into FleetStatus for the support console + OTA version comparison."""
    ts = now()
    if any(v is not None for v in (body.fw_c3, body.fw_c6, body.heap_free, body.role)):
        fs = db.get(FleetStatus, tenant_id)
        if not fs:
            fs = FleetStatus(tenant_id=tenant_id)
            db.add(fs)
        if body.fw_c3 is not None:
            fs.fw_c3 = body.fw_c3
        if body.fw_c6 is not None:
            fs.fw_c6 = body.fw_c6
        if body.heap_free is not None:
            fs.heap_free = body.heap_free
        if body.role is not None:
            fs.role = body.role[:16]
        fs.updated_at = ts
    items: list[tuple[str, str]] = []
    for n in body.nodes:
        eui = str(n.get("eui", "")).strip().lower()
        if eui:
            kind = "gateway" if str(n.get("role", "R")).upper() == "G" else "router"
            items.append((eui, kind))
    for r in body.routers:   # legacy clients
        eui = str(r.get("eui", "")).strip().lower()
        if eui:
            items.append((eui, "router"))

    for eui, kind in items:
        row = db.scalar(select(MeshNode).where(MeshNode.tenant_id == tenant_id, MeshNode.eui == eui))
        if row:
            row.last_seen = ts
            row.kind = kind
        else:
            db.add(MeshNode(tenant_id=tenant_id, eui=eui, kind=kind, last_seen=ts))
    db.commit()
    return {"ok": True, "nodes": len(items)}


# ---- topology sync (replaces app-local SharedPreferences) ------------------ #

class TopologyBody(BaseModel):
    topology: dict


@app.get("/v1/topology")
def get_topology(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    row = db.get(Topology, p.tenant_id)
    return {"topology": json.loads(row.json) if row else {"racks": []},
            "updated_at": row.updated_at if row else 0.0}


@app.put("/v1/topology")
def put_topology(body: TopologyBody, p: Principal = Depends(current_principal),
                 db: Session = Depends(get_db)):
    row = db.get(Topology, p.tenant_id)
    payload = json.dumps(body.topology)
    if row:
        row.json = payload
        row.updated_at = now()
    else:
        db.add(Topology(tenant_id=p.tenant_id, json=payload, updated_at=now()))
    db.commit()
    mapped = rebuild_sensor_map(db, p.tenant_id, body.topology)
    return {"ok": True, "mapped_sensors": mapped}


# ---- commissioned-device roster (tenant-scoped, survives phone changes) ---- #

class DevicesBody(BaseModel):
    devices: list[dict] = []   # [{"eui","kind","role","name"}]


@app.get("/v1/devices")
def get_devices(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(CommissionedDevice).where(CommissionedDevice.tenant_id == p.tenant_id)).all()
    return {"devices": [{"eui": r.eui, "kind": r.kind, "role": r.role, "name": r.name}
                        for r in rows]}


@app.put("/v1/devices")
def put_devices(body: DevicesBody, p: Principal = Depends(current_principal),
                db: Session = Depends(get_db)):
    """Additive upsert (merge): adds/updates each device in the payload but never
    removes ones not listed — so a phone with only a partial view (e.g. cloud-only,
    no BLE) can't wipe the roster. Use DELETE to remove."""
    n = 0
    for d in body.devices:
        eui = str(d.get("eui", "")).strip().lower()
        if not valid_eui(eui):
            continue   # never store a malformed EUI in the roster
        kind = str(d.get("kind", "sensor"))
        role = str(d.get("role", ""))
        name = str(d.get("name", "")).strip()
        row = db.scalar(select(CommissionedDevice).where(
            CommissionedDevice.tenant_id == p.tenant_id, CommissionedDevice.eui == eui))
        if row:
            row.kind, row.role = kind, role
            if name:                  # only set a name, never blank one another phone set
                row.name = name
        else:
            db.add(CommissionedDevice(id=new_id(), tenant_id=p.tenant_id, eui=eui,
                                      kind=kind, role=role, name=name, added_at=now()))
        n += 1
    db.commit()
    return {"ok": True, "count": n}


@app.delete("/v1/devices/{eui}")
def delete_device(eui: str, p: Principal = Depends(current_principal),
                  db: Session = Depends(get_db)):
    db.execute(delete(CommissionedDevice).where(
        CommissionedDevice.tenant_id == p.tenant_id,
        CommissionedDevice.eui == eui.strip().lower()))
    db.commit()
    return {"ok": True}


# ---- thresholds ------------------------------------------------------------ #

class ThresholdBody(BaseModel):
    scope: str = "tenant"          # tenant|rack|port
    scope_id: str = ""
    high_c: float
    delta_c: float
    enabled: bool = True


@app.get("/v1/thresholds")
def get_thresholds(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    rows = db.scalars(select(Threshold).where(Threshold.tenant_id == p.tenant_id)).all()
    return {
        "defaults": {"high_c": config.DEFAULT_HIGH_C, "delta_c": config.DEFAULT_DELTA_C},
        "thresholds": [{"scope": t.scope, "scope_id": t.scope_id, "high_c": t.high_c,
                        "delta_c": t.delta_c, "enabled": t.enabled} for t in rows],
    }


@app.put("/v1/thresholds")
def put_threshold(body: ThresholdBody, p: Principal = Depends(require_admin),
                  db: Session = Depends(get_db)):
    if body.scope not in ("tenant", "rack", "port"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "scope must be tenant|rack|port")
    existing = db.scalar(select(Threshold).where(
        Threshold.tenant_id == p.tenant_id, Threshold.scope == body.scope,
        Threshold.scope_id == body.scope_id,
    ))
    if existing:
        existing.high_c, existing.delta_c, existing.enabled = body.high_c, body.delta_c, body.enabled
    else:
        db.add(Threshold(id=new_id(), tenant_id=p.tenant_id, scope=body.scope,
                         scope_id=body.scope_id, high_c=body.high_c,
                         delta_c=body.delta_c, enabled=body.enabled))
    db.commit()
    return {"ok": True}


# ---- current temps + alerts ------------------------------------------------ #

@app.get("/v1/current")
def current(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    """Latest reading per sensor, expanded to one row per mapped probe (for the
    app's status view). Each row carries the probe's own temperature + location;
    an unmapped sensor yields a single row at its hottest probe so it's still
    visible and assignable."""
    euis = db.scalars(select(Reading.eui).where(Reading.tenant_id == p.tenant_id).distinct()).all()
    out = []
    for eui in euis:
        r = db.scalar(select(Reading).where(Reading.tenant_id == p.tenant_id, Reading.eui == eui)
                      .order_by(Reading.ts.desc()).limit(1))
        probes = json.loads(r.probes)                          # [{"rom","c"}] (or legacy [float])
        temp_by_rom = {pr["rom"]: pr["c"] for pr in probes if isinstance(pr, dict)}
        sms = db.scalars(select(SensorMap).where(
            SensorMap.tenant_id == p.tenant_id, SensorMap.eui == eui)).all()
        if not sms:
            out.append({"eui": eui, "rom": "", "ts": r.ts, "temp": r.max_c, "max_c": r.max_c,
                        "probes": probes, "location": "", "box": r.box, "slot": r.slot})
            continue
        for sm in sms:
            temp = temp_by_rom.get(sm.probe_rom) if sm.probe_rom else r.max_c
            out.append({"eui": eui, "rom": sm.probe_rom, "ts": r.ts, "temp": temp,
                        "max_c": r.max_c, "location": sm.label, "box": sm.box, "slot": sm.slot})
    return {"sensors": out}


# ---- tenant settings (alert granularity) ----------------------------------- #

class SettingsBody(BaseModel):
    alert_granularity: str = "sensor"        # sensor|probe
    collect_interval_s: int | None = None    # how often devices sample/forward (s)


@app.get("/v1/settings")
def get_settings(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    t = db.get(Tenant, p.tenant_id)
    return {
        "alert_granularity": (t.alert_granularity if t else "sensor"),
        "collect_interval_s": (t.collect_interval_s if t else 60),
    }


@app.put("/v1/settings")
def put_settings(body: SettingsBody, p: Principal = Depends(require_admin),
                 db: Session = Depends(get_db)):
    if body.alert_granularity not in ("sensor", "probe"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "alert_granularity must be sensor|probe")
    t = db.get(Tenant, p.tenant_id)
    if t:
        t.alert_granularity = body.alert_granularity
        if body.collect_interval_s is not None:
            t.collect_interval_s = max(10, min(3600, int(body.collect_interval_s)))
        db.commit()
    return {"ok": True, "alert_granularity": t.alert_granularity if t else body.alert_granularity,
            "collect_interval_s": t.collect_interval_s if t else 60}


# ---- environmental data (router/gateway BME) + CSV export ------------------ #

def _num(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _csv(s: str) -> str:
    """Minimal CSV field quoting."""
    s = str(s)
    if any(c in s for c in (",", '"', "\n", "\r")):
        return '"' + s.replace('"', '""') + '"'
    return s


def _device_names(db: Session, tenant_id: str) -> dict:
    return {d.eui: d.name for d in db.scalars(
        select(CommissionedDevice).where(CommissionedDevice.tenant_id == tenant_id)).all()}


class EnvBody(BaseModel):
    sensor_id: str                       # the router/gateway EUI
    temp: float | None = None
    hum: float | None = None
    pres: float | None = None
    voc: float | None = None
    ts: float | None = None


@app.post("/v1/env")
def ingest_env(body: EnvBody, tenant_id: str = Depends(tenant_from_api_key),
               db: Session = Depends(get_db)):
    """A router/gateway posts a BME environmental sample (via the gateway's key)."""
    eui = body.sensor_id.strip().lower()
    if not valid_eui(eui):
        return {"ok": False, "error": "invalid sensor_id", "eui": eui}
    db.add(EnvReading(tenant_id=tenant_id, ts=(body.ts or now()), eui=eui,
                      temp=_num(body.temp), hum=_num(body.hum),
                      pres=_num(body.pres), voc=_num(body.voc)))
    db.commit()
    return {"ok": True, "eui": eui}


@app.get("/v1/env/current")
def env_current(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    """Latest BME sample per router/gateway, for the app/web env tab."""
    names = _device_names(db, p.tenant_id)
    euis = db.scalars(select(EnvReading.eui).where(
        EnvReading.tenant_id == p.tenant_id).distinct()).all()
    out = []
    for eui in euis:
        r = db.scalar(select(EnvReading).where(
            EnvReading.tenant_id == p.tenant_id, EnvReading.eui == eui)
            .order_by(EnvReading.ts.desc()).limit(1))
        out.append({"eui": eui, "name": names.get(eui, ""), "ts": r.ts,
                    "temp": r.temp, "hum": r.hum, "pres": r.pres, "voc": r.voc})
    return {"env": out}


@app.get("/v1/env/probes")
def env_probes(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    """Every probe of every COMMISSIONED/mapped sensor's latest reading — for the
    Environment & Logs view. Each probe carries its temp + a label (its mapped
    location, or 'Probe N' if that probe isn't assigned). Sensors with no mapping
    at all are excluded (no bare raw-EUI rows)."""
    names = _device_names(db, p.tenant_id)
    mapped_euis = set(db.scalars(select(SensorMap.eui).where(
        SensorMap.tenant_id == p.tenant_id).distinct()).all())
    out = []
    for eui in mapped_euis:
        r = db.scalar(select(Reading).where(
            Reading.tenant_id == p.tenant_id, Reading.eui == eui)
            .order_by(Reading.ts.desc()).limit(1))
        if not r:
            continue
        try:
            probes = json.loads(r.probes)
        except (ValueError, TypeError):
            probes = []
        loc_by_rom = {sm.probe_rom: sm.label for sm in db.scalars(select(SensorMap).where(
            SensorMap.tenant_id == p.tenant_id, SensorMap.eui == eui)).all()}
        for i, pr in enumerate(probes):
            if not isinstance(pr, dict):
                continue
            rom = pr.get("rom", "")
            loc = loc_by_rom.get(rom, "")
            label = loc if loc else f"Probe {i + 1}" + (f" · …{rom[-4:]}" if rom else "")
            out.append({"eui": eui, "name": names.get(eui, ""), "rom": rom,
                        "temp": pr.get("c"), "location": loc, "label": label, "ts": r.ts})
    return {"probes": out}


@app.get("/v1/env/export.csv")
def env_export(p: Principal = Depends(current_principal), db: Session = Depends(get_db),
               start: float | None = None, end: float | None = None):
    """All router BME samples as CSV (one row per sample), with device names."""
    names = _device_names(db, p.tenant_id)
    q = select(EnvReading).where(EnvReading.tenant_id == p.tenant_id)
    if start is not None:
        q = q.where(EnvReading.ts >= start)
    if end is not None:
        q = q.where(EnvReading.ts <= end)
    rows = db.scalars(q.order_by(EnvReading.ts.asc())).all()
    lines = ["timestamp,device,eui,temp_c,humidity_pct,pressure_hpa,voc\n"]
    for r in rows:
        name = names.get(r.eui) or r.eui
        lines.append(f"{_iso(r.ts)},{_csv(name)},{r.eui},"
                     f"{r.temp:.2f},{r.hum:.2f},{r.pres:.2f},{r.voc:.2f}\n")
    return Response("".join(lines), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=routers_env.csv"})


@app.get("/v1/readings/export.csv")
def readings_export(p: Principal = Depends(current_principal), db: Session = Depends(get_db),
                    start: float | None = None, end: float | None = None):
    """All sensor (DS18B20) readings as CSV, one row per probe, with names."""
    names = _device_names(db, p.tenant_id)
    q = select(Reading).where(Reading.tenant_id == p.tenant_id)
    if start is not None:
        q = q.where(Reading.ts >= start)
    if end is not None:
        q = q.where(Reading.ts <= end)
    rows = db.scalars(q.order_by(Reading.ts.asc())).all()
    lines = ["timestamp,device,eui,probe_rom,temp_c,max_c\n"]
    for r in rows:
        name = names.get(r.eui) or r.eui
        iso = _iso(r.ts)
        try:
            probes = json.loads(r.probes)
        except (ValueError, TypeError):
            probes = []
        for pr in probes:
            if not isinstance(pr, dict):
                continue
            c = pr.get("c")
            lines.append(f"{iso},{_csv(name)},{r.eui},{pr.get('rom','')},"
                         f"{'' if c is None else f'{float(c):.2f}'},{r.max_c:.2f}\n")
    return Response("".join(lines), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=sensors.csv"})


# ---- firmware crash reports ------------------------------------------------ #

class CrashBody(BaseModel):
    sensor_id: str               # the crashing device's EUI
    reset_reason: str = ""
    fw: str = ""
    pc: str = ""
    backtrace: str = ""
    detail: str = ""
    ts: float | None = None


@app.post("/v1/crashes")
def ingest_crash(body: CrashBody, tenant_id: str = Depends(tenant_from_api_key),
                 db: Session = Depends(get_db)):
    """A device forwards a firmware panic report (gateway directly; routers relay
    via the gateway). Authenticated by the gateway's X-API-Key."""
    eui = body.sensor_id.strip().lower()
    if not valid_eui(eui):
        return {"ok": False, "error": "invalid sensor_id", "eui": eui}
    db.add(CrashReport(id=new_id(), tenant_id=tenant_id, eui=eui, ts=(body.ts or now()),
                       reset_reason=body.reset_reason[:40], fw=body.fw[:40], pc=body.pc[:20],
                       backtrace=body.backtrace[:4000], detail=body.detail[:8000]))
    db.commit()
    return {"ok": True, "eui": eui}


# A device re-uploads the SAME crash every 20s until it gives up (6 tries), because
# the C3 counts attempts, not confirmed deliveries — so one panic lands as ~6 rows.
# Collapse identical consecutive reports into a single entry carrying `occurrences`,
# so the count reflects real crashes instead of retry noise. Purely a read-side view:
# every row is still stored, and the CSV export stays raw for forensics.
CRASH_DEDUP_WINDOW_S = 300.0


@app.get("/v1/crashes")
def list_crashes(p: Principal = Depends(require_admin), db: Session = Depends(get_db),
                 raw: bool = False):
    rows = db.scalars(select(CrashReport).where(CrashReport.tenant_id == p.tenant_id)
                      .order_by(CrashReport.ts.desc()).limit(2000)).all()

    def rec(c: CrashReport) -> dict:
        return {"id": c.id, "eui": c.eui, "ts": c.ts, "reset_reason": c.reset_reason,
                "fw": c.fw, "pc": c.pc, "backtrace": c.backtrace, "detail": c.detail}

    if raw:
        return {"crashes": [rec(c) for c in rows[:500]]}

    out: list[dict] = []
    for c in rows:                       # newest -> oldest
        sig = (c.eui, c.reset_reason, c.pc, c.backtrace)
        if out and out[-1]["_sig"] == sig and abs(out[-1]["first_ts"] - c.ts) <= CRASH_DEDUP_WINDOW_S:
            out[-1]["occurrences"] += 1
            out[-1]["first_ts"] = c.ts   # rows descend, so this walks back to the earliest retry
            continue
        e = rec(c)
        e["occurrences"] = 1
        e["first_ts"] = c.ts
        e["_sig"] = sig
        out.append(e)
        if len(out) >= 500:
            break
    for e in out:
        e.pop("_sig", None)
    return {"crashes": out}


@app.get("/v1/fleet")
def fleet_status(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    """Gateway self-report (firmware versions, free heap, role) that arrives with the
    30s mesh push. Surfaced so a customer can watch heap headroom — a heap that
    sawtooths down before each panic is the signature of a leak in the gateway."""
    fs = db.get(FleetStatus, p.tenant_id)
    if not fs:
        return {"fleet": None}
    return {"fleet": {"fw_c3": fs.fw_c3, "fw_c6": fs.fw_c6, "heap_free": fs.heap_free,
                      "role": fs.role, "updated_at": fs.updated_at}}


@app.get("/v1/crashes/export.csv")
def crashes_export(p: Principal = Depends(require_admin), db: Session = Depends(get_db)):
    names = _device_names(db, p.tenant_id)
    rows = db.scalars(select(CrashReport).where(CrashReport.tenant_id == p.tenant_id)
                      .order_by(CrashReport.ts.desc())).all()
    lines = ["timestamp,device,eui,reset_reason,fw,pc,backtrace\n"]
    for c in rows:
        name = names.get(c.eui) or c.eui
        lines.append(f"{_iso(c.ts)},{_csv(name)},{c.eui},{_csv(c.reset_reason)},"
                     f"{_csv(c.fw)},{c.pc},{_csv(c.backtrace)}\n")
    return Response("".join(lines), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=crashes.csv"})


@app.get("/v1/routers")
def list_routers(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    """Routers the gateway has reported, with online/offline from last_seen.
    Online if seen within the same stale window the watchdog uses for sensors."""
    cutoff = now() - config.STALE_AFTER_S
    rows = db.scalars(select(MeshNode).where(MeshNode.tenant_id == p.tenant_id)
                      .order_by(MeshNode.last_seen.desc())).all()
    return {"routers": [{"eui": m.eui, "kind": m.kind, "last_seen": m.last_seen,
                         "online": m.last_seen >= cutoff} for m in rows]}


@app.get("/v1/alerts")
def list_alerts(state: str = "open", p: Principal = Depends(current_principal),
                db: Session = Depends(get_db)):
    q = select(Alert).where(Alert.tenant_id == p.tenant_id)
    if state == "open":
        q = q.where(Alert.state.in_(("open", "acked")))
    elif state != "all":
        q = q.where(Alert.state == state)
    rows = db.scalars(q.order_by(Alert.opened_at.desc()).limit(500)).all()
    return {"alerts": [{"id": a.id, "kind": a.kind, "state": a.state, "location": a.location,
                        "value": a.value, "threshold": a.threshold, "opened_at": a.opened_at,
                        "cleared_at": a.cleared_at} for a in rows]}


@app.post("/v1/alerts/{alert_id}/ack")
def ack_alert(alert_id: str, p: Principal = Depends(current_principal),
              db: Session = Depends(get_db)):
    a = db.get(Alert, alert_id)
    if not a or a.tenant_id != p.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert not found")
    if a.state == "open":
        a.state = "acked"
        db.commit()
    return {"ok": True, "state": a.state}


# ---- tenant alert recipients ----------------------------------------------- #

class RecipientsBody(BaseModel):
    alert_emails: str = ""        # comma-separated
    alert_phones: str = ""


@app.get("/v1/recipients")
def get_recipients(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    """The extra alert addresses on the tenant, so a client can EDIT them instead
    of overwriting blind.

    There was no read endpoint at all, which is why the dashboard's editor opened
    empty every time: submitting it replaced the whole list with whatever was in
    the box, silently deleting every configured alert email. Also reports the
    opted-in members, so the UI can show who actually gets notified."""
    t = db.get(Tenant, p.tenant_id)
    emails, phones = recipients_for(db, p.tenant_id)
    return {
        "alert_emails": (t.alert_emails or "") if t else "",
        "alert_phones": (t.alert_phones or "") if t else "",
        # The full effective set = opted-in members + the extras above.
        "effective_emails": emails,
        "effective_phones": phones,
    }


@app.put("/v1/recipients")
def set_recipients(body: RecipientsBody, p: Principal = Depends(require_admin),
                   db: Session = Depends(get_db)):
    t = db.get(Tenant, p.tenant_id)
    t.alert_emails, t.alert_phones = body.alert_emails, body.alert_phones
    db.commit()
    return {"ok": True}


# ---- notification delivery (is alerting actually reaching anyone?) --------- #

def _delivery_channels() -> dict:
    """Which channel notify_email/notify_sms will actually use.

    Delivery falls through SES -> SMTP -> log, and notify_email NEVER raises. So
    an appliance with no mail configured looks completely healthy while every
    alert it 'sends' is only printed to a log file. Exposing this lets the
    dashboard say so out loud. Never returns the password."""
    if config.SES_FROM:
        email = "ses"
    elif config.SMTP_HOST:
        email = "smtp"
    else:
        email = "log-only"
    if config.SNS_SMS_ENABLED:
        sms = "sns"
    elif config.TWILIO_ACCOUNT_SID and config.TWILIO_FROM:
        sms = "twilio"
    else:
        sms = "log-only"
    return {
        "email": email,
        "email_configured": email != "log-only",
        "email_from": config.MAIL_FROM,
        "smtp_host": config.SMTP_HOST or "",
        "sms": sms,
        "sms_configured": sms != "log-only",
    }


@app.get("/v1/notifications/status")
def notifications_status(p: Principal = Depends(require_admin)):
    """Admin-visible delivery status, so 'alerts are configured' can be verified
    instead of assumed."""
    return _delivery_channels()


@app.post("/v1/notifications/test")
def notifications_test(p: Principal = Depends(require_admin), db: Session = Depends(get_db)):
    """Send a test alert to the calling admin's own address and report what
    happened — a one-click check that beats waiting for a real overheat to
    discover the mail path was never working."""
    u = db.get(User, p.user_id)
    if not u or not u.email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Your account has no email address.")
    ch = _delivery_channels()
    if not ch["email_configured"]:
        # Be explicit rather than returning ok:true for a message that only got
        # written to a log file.
        return {
            "ok": False,
            "channel": "log-only",
            "detail": "No SES_FROM and no SMTP_HOST configured — the test was written to the "
                      "server log instead of being emailed. Alerts are not reaching anyone.",
        }
    notify_email(
        [u.email],
        "HVAC Monitor test alert",
        "This is a test from your HVAC Monitor dashboard.\n\n"
        "If you received it, alert delivery is working.",
    )
    return {"ok": True, "channel": ch["email"], "sent_to": u.email, "detail": "Test email dispatched."}


# --------------------------------------------------------------------------- #
# Manufacturer field-support plane — gated by SUPPORT_TOKEN (X-Support-Token).  #
# Read-only fleet diagnostics ACROSS tenants on this appliance + firmware        #
# publish. Disabled (404) unless config.SUPPORT_TOKEN is set.                    #
# --------------------------------------------------------------------------- #

def _audit(db: Session, action: str, detail: str = "") -> None:
    """Record manufacturer support access so the customer can review it later."""
    db.add(SupportAudit(action=action[:60], detail=detail[:500]))


def _tenant_names(db: Session) -> dict:
    return {t.id: t.name for t in db.scalars(select(Tenant)).all()}


@app.get("/v1/support/overview")
def support_overview(sp: SupportPrincipal = Depends(support_principal),
                     db: Session = Depends(get_db)):
    """Cross-tenant fleet health: per-tenant gateway firmware/heap/role, mesh
    roster + online state, and crash/alert counts."""
    cutoff = now() - config.STALE_AFTER_S
    out = []
    for t in db.scalars(select(Tenant)).all():
        fs = db.get(FleetStatus, t.id)
        nodes = db.scalars(select(MeshNode).where(MeshNode.tenant_id == t.id)
                           .order_by(MeshNode.last_seen.desc())).all()
        crash_n = db.scalar(select(func.count()).select_from(CrashReport)
                            .where(CrashReport.tenant_id == t.id)) or 0
        open_n = db.scalar(select(func.count()).select_from(Alert).where(
            Alert.tenant_id == t.id, Alert.state.in_(("open", "acked")))) or 0
        out.append({
            "tenant_id": t.id, "tenant": t.name,
            "fw_c3": fs.fw_c3 if fs else 0, "fw_c6": fs.fw_c6 if fs else 0,
            "heap_free": fs.heap_free if fs else 0, "role": fs.role if fs else "",
            "status_age_s": (now() - fs.updated_at) if fs else None,
            "nodes": [{"eui": m.eui, "kind": m.kind, "last_seen": m.last_seen,
                       "online": m.last_seen >= cutoff} for m in nodes],
            "crash_count": int(crash_n), "open_alerts": int(open_n),
        })
    _audit(db, "read.overview", f"{len(out)} tenants")
    db.commit()
    return {"tenants": out}


@app.get("/v1/support/crashes")
def support_crashes(sp: SupportPrincipal = Depends(support_principal),
                    db: Session = Depends(get_db), format: str = "json"):
    rows = db.scalars(select(CrashReport).order_by(CrashReport.ts.desc()).limit(2000)).all()
    tnames = _tenant_names(db)
    if format == "csv":
        lines = ["timestamp,tenant,eui,reset_reason,fw,pc,backtrace\n"]
        for c in rows:
            lines.append(f"{_iso(c.ts)},{_csv(tnames.get(c.tenant_id, ''))},{c.eui},"
                         f"{_csv(c.reset_reason)},{_csv(c.fw)},{c.pc},{_csv(c.backtrace)}\n")
        _audit(db, "read.crashes.csv", f"{len(rows)} rows")
        db.commit()
        return Response("".join(lines), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=fleet_crashes.csv"})
    _audit(db, "read.crashes", f"{len(rows)} rows")
    db.commit()
    return {"crashes": [{"id": c.id, "tenant": tnames.get(c.tenant_id, ""), "eui": c.eui,
                         "ts": c.ts, "reset_reason": c.reset_reason, "fw": c.fw, "pc": c.pc,
                         "backtrace": c.backtrace, "detail": c.detail} for c in rows]}


@app.get("/v1/support/env")
def support_env(sp: SupportPrincipal = Depends(support_principal),
                db: Session = Depends(get_db), format: str = "json", limit: int = 5000):
    rows = db.scalars(select(EnvReading).order_by(EnvReading.ts.desc())
                      .limit(min(limit, 20000))).all()
    tnames = _tenant_names(db)
    if format == "csv":
        lines = ["timestamp,tenant,eui,temp_c,humidity_pct,pressure_hpa,voc\n"]
        for r in rows:
            lines.append(f"{_iso(r.ts)},{_csv(tnames.get(r.tenant_id, ''))},{r.eui},"
                         f"{r.temp:.2f},{r.hum:.2f},{r.pres:.2f},{r.voc:.2f}\n")
        _audit(db, "read.env.csv", f"{len(rows)} rows")
        db.commit()
        return Response("".join(lines), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=fleet_env.csv"})
    _audit(db, "read.env", f"{len(rows)} rows")
    db.commit()
    return {"env": [{"tenant": tnames.get(r.tenant_id, ""), "eui": r.eui, "ts": r.ts,
                     "temp": r.temp, "hum": r.hum, "pres": r.pres, "voc": r.voc} for r in rows]}


@app.get("/v1/support/readings")
def support_readings(sp: SupportPrincipal = Depends(support_principal),
                     db: Session = Depends(get_db), format: str = "json", limit: int = 5000):
    rows = db.scalars(select(Reading).order_by(Reading.ts.desc())
                      .limit(min(limit, 20000))).all()
    tnames = _tenant_names(db)
    if format == "csv":
        lines = ["timestamp,tenant,eui,probe_rom,temp_c,max_c\n"]
        for r in rows:
            try:
                probes = json.loads(r.probes)
            except (ValueError, TypeError):
                probes = []
            for pr in probes:
                if not isinstance(pr, dict):
                    continue
                c = pr.get("c")
                lines.append(f"{_iso(r.ts)},{_csv(tnames.get(r.tenant_id, ''))},{r.eui},"
                             f"{pr.get('rom', '')},{'' if c is None else f'{float(c):.2f}'},{r.max_c:.2f}\n")
        _audit(db, "read.readings.csv", f"{len(rows)} rows")
        db.commit()
        return Response("".join(lines), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=fleet_readings.csv"})
    _audit(db, "read.readings", f"{len(rows)} rows")
    db.commit()
    return {"readings": [{"tenant": tnames.get(r.tenant_id, ""), "eui": r.eui, "ts": r.ts,
                          "max_c": r.max_c, "probes": r.probes} for r in rows]}


@app.get("/v1/support/alerts")
def support_alerts(sp: SupportPrincipal = Depends(support_principal),
                   db: Session = Depends(get_db)):
    rows = db.scalars(select(Alert).order_by(Alert.opened_at.desc()).limit(1000)).all()
    tnames = _tenant_names(db)
    _audit(db, "read.alerts", f"{len(rows)} rows")
    db.commit()
    return {"alerts": [{"id": a.id, "tenant": tnames.get(a.tenant_id, ""), "eui": a.eui,
                        "kind": a.kind, "state": a.state, "location": a.location,
                        "value": a.value, "threshold": a.threshold,
                        "opened_at": a.opened_at, "cleared_at": a.cleared_at} for a in rows]}


# ---- firmware releases + hosting (manufacturer publishes; gateways pull) ---- #

def _latest_release(db: Session, kind: str) -> FirmwareRelease | None:
    return db.scalar(select(FirmwareRelease).where(FirmwareRelease.kind == kind)
                     .order_by(FirmwareRelease.version.desc(),
                               FirmwareRelease.created_at.desc()).limit(1))


def _write_manifest(db: Session) -> dict:
    """Regenerate firmware/manifest.json from the newest release per chip, in the
    exact shape the C3 OTA reader expects (c3_version/c3file, c6_version/c6file)
    plus severity + sha256 for the tiered rollout + integrity."""
    os.makedirs(config.FIRMWARE_DIR, exist_ok=True)
    c3, c6 = _latest_release(db, "c3"), _latest_release(db, "c6")
    manifest = {
        "c3_version": c3.version if c3 else 0, "c3file": c3.filename if c3 else "",
        "c3_severity": c3.severity if c3 else "optional", "c3_sha256": c3.sha256 if c3 else "",
        "c6_version": c6.version if c6 else 0, "c6file": c6.filename if c6 else "",
        "c6_severity": c6.severity if c6 else "optional", "c6_sha256": c6.sha256 if c6 else "",
    }
    with open(os.path.join(config.FIRMWARE_DIR, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    return manifest


@app.post("/v1/support/firmware")
async def publish_firmware(request: Request, kind: str, version: int,
                           severity: str = "optional", stage: str = "full", notes: str = "",
                           sp: SupportPrincipal = Depends(support_principal),
                           db: Session = Depends(get_db)):
    """Publish a firmware image. Metadata in the query string, the raw .bin as the
    request body (no multipart dep). `stage=canary` rolls only the gateway first
    (verify-first) until promoted; `full` rolls the fleet. Stores the bin + a
    FirmwareRelease row and rewrites the manifest the fleet polls."""
    kind = kind.lower().strip()
    severity = severity.lower().strip()
    stage = stage.lower().strip()
    if kind not in ("c3", "c6"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "kind must be c3 or c6")
    if severity not in ("mandatory", "optional"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "severity must be mandatory or optional")
    if stage not in ("canary", "full"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "stage must be canary or full")
    data = await request.body()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty firmware body")
    os.makedirs(config.FIRMWARE_DIR, exist_ok=True)
    filename = f"{kind}_v{version}.bin"
    with open(os.path.join(config.FIRMWARE_DIR, filename), "wb") as fh:
        fh.write(data)
    sha = hashlib.sha256(data).hexdigest()
    db.add(FirmwareRelease(kind=kind, version=version, severity=severity, stage=stage,
                           filename=filename, size=len(data), sha256=sha, notes=notes[:2000]))
    _audit(db, "firmware.publish", f"{kind} v{version} {severity}/{stage} {len(data)}B")
    db.commit()
    manifest = _write_manifest(db)
    return {"ok": True, "kind": kind, "version": version, "stage": stage, "filename": filename,
            "size": len(data), "sha256": sha, "manifest": manifest}


class OtaPromoteBody(BaseModel):
    kind: str
    version: int


@app.post("/v1/support/ota/promote")
def promote_firmware(body: OtaPromoteBody, sp: SupportPrincipal = Depends(support_principal),
                     db: Session = Depends(get_db)):
    """Promote a canary release to full so the gateway rolls it to the whole fleet
    (do this after the gateway reports the new version healthy)."""
    kind = body.kind.lower().strip()
    if kind not in ("c3", "c6"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "kind must be c3 or c6")
    rel = db.scalar(select(FirmwareRelease).where(
        FirmwareRelease.kind == kind, FirmwareRelease.version == body.version))
    if not rel:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "release not found")
    rel.stage = "full"
    _audit(db, "ota.promote", f"{kind} v{body.version} -> full")
    db.commit()
    return {"ok": True, "kind": kind, "version": body.version, "stage": "full"}


@app.get("/v1/support/firmware")
def list_firmware(sp: SupportPrincipal = Depends(support_principal),
                  db: Session = Depends(get_db)):
    rows = db.scalars(select(FirmwareRelease)
                      .order_by(FirmwareRelease.created_at.desc()).limit(200)).all()
    return {"releases": [{"id": r.id, "kind": r.kind, "version": r.version,
                          "severity": r.severity, "stage": r.stage, "filename": r.filename,
                          "size": r.size, "sha256": r.sha256, "notes": r.notes,
                          "created_at": r.created_at} for r in rows]}


@app.get("/firmware/manifest.json")
def firmware_manifest():
    """Served to the gateway's OTA poll. Defined before the SPA catch-all so it
    isn't shadowed (and `firmware` is in _API_PREFIXES below)."""
    path = os.path.join(config.FIRMWARE_DIR, "manifest.json")
    if os.path.isfile(path):
        return FileResponse(path, media_type="application/json")
    return {"c3_version": 0, "c3file": "", "c6_version": 0, "c6file": ""}


@app.get("/firmware/{filename}")
def firmware_file(filename: str):
    safe = os.path.normpath(os.path.join(config.FIRMWARE_DIR, filename))
    if not safe.startswith(config.FIRMWARE_DIR) or not os.path.isfile(safe):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "firmware not found")
    return FileResponse(safe, media_type="application/octet-stream")


# ---- tiered OTA orchestration ---------------------------------------------- #

@app.get("/v1/ota/check")
def ota_check(tenant_id: str = Depends(tenant_from_api_key), db: Session = Depends(get_db)):
    """The gateway polls this. Returns the current target per chip + severity, and
    whether this tenant's admin has approved an OPTIONAL version. The gateway
    builds the image URL from its own configured cloud URL (`/firmware/<file>`)."""
    c3, c6 = _latest_release(db, "c3"), _latest_release(db, "c6")
    st = db.get(OtaState, tenant_id)
    return {
        "c3_version": c3.version if c3 else 0, "c3file": c3.filename if c3 else "",
        "c3_severity": c3.severity if c3 else "optional", "c3_stage": c3.stage if c3 else "full",
        "c6_version": c6.version if c6 else 0, "c6file": c6.filename if c6 else "",
        "c6_severity": c6.severity if c6 else "optional", "c6_stage": c6.stage if c6 else "full",
        "approved_c3": st.approved_c3 if st else 0,
        "approved_c6": st.approved_c6 if st else 0,
    }


@app.get("/v1/ota/available")
def ota_available(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    """The customer app polls this — surfaces an OPTIONAL update newer than the
    fleet's current firmware so the user can choose to apply it. Mandatory updates
    aren't listed (they auto-apply)."""
    fs = db.get(FleetStatus, p.tenant_id)
    st = db.get(OtaState, p.tenant_id)
    out = []
    for kind, cur, appr in (("c3", fs.fw_c3 if fs else 0, st.approved_c3 if st else 0),
                            ("c6", fs.fw_c6 if fs else 0, st.approved_c6 if st else 0)):
        rel = _latest_release(db, kind)
        if rel and rel.severity == "optional" and rel.version > cur:
            out.append({"kind": kind, "version": rel.version, "current": cur,
                        "notes": rel.notes, "approved": appr >= rel.version})
    return {"updates": out}


class OtaApproveBody(BaseModel):
    kind: str
    version: int


@app.post("/v1/ota/approve")
def ota_approve(body: OtaApproveBody, p: Principal = Depends(require_admin),
                db: Session = Depends(get_db)):
    """The customer admin approves an optional update; the gateway applies it on
    its next poll."""
    kind = body.kind.lower().strip()
    if kind not in ("c3", "c6"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "kind must be c3 or c6")
    st = db.get(OtaState, p.tenant_id)
    if not st:
        st = OtaState(tenant_id=p.tenant_id)
        db.add(st)
    if kind == "c3":
        st.approved_c3 = body.version
    else:
        st.approved_c6 = body.version
    st.updated_at = now()
    db.commit()
    return {"ok": True, "kind": kind, "approved_version": body.version}


@app.get("/v1/support-access")
def support_access_log(p: Principal = Depends(require_admin), db: Session = Depends(get_db)):
    """Customer-visible audit of manufacturer support-token access to this
    appliance (transparency/trust)."""
    rows = db.scalars(select(SupportAudit).order_by(SupportAudit.ts.desc()).limit(500)).all()
    return {"access": [{"ts": a.ts, "action": a.action, "detail": a.detail} for a in rows]}


@app.get("/health")
def health():
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Web dashboard (optional) — serve the built React app at the root            #
# --------------------------------------------------------------------------- #
# If the dashboard has been built (web-dashboard/dist), serve it from the same
# origin as the API. Same-origin means the browser needs NO CORS, and there's a
# single URL to hand out. Build it with `npm run build` in ../web-dashboard, or
# point WEB_DIR at the built dir. This block is a no-op if the dir is absent.
_HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.abspath(
    os.environ.get("WEB_DIR", os.path.join(_HERE, "..", "web-dashboard", "dist"))
)
_API_PREFIXES = ("v1/", "v1", "health", "docs", "redoc", "openapi.json", "firmware/", "firmware")

if os.path.isdir(WEB_DIR):
    _INDEX = os.path.join(WEB_DIR, "index.html")

    @app.get("/")
    def _spa_root():
        return FileResponse(_INDEX)

    @app.get("/{full_path:path}")
    def _spa(full_path: str):
        # Never shadow the API — let unknown API paths 404 as usual.
        if full_path.startswith(_API_PREFIXES):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
        # Serve a real built asset if it exists (guard against path traversal);
        # otherwise fall back to index.html so client-side routing works.
        candidate = os.path.normpath(os.path.join(WEB_DIR, full_path))
        if candidate.startswith(WEB_DIR) and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(_INDEX)

    print(f"[web] serving dashboard from {WEB_DIR} at /")
else:
    print(f"[web] dashboard not built ({WEB_DIR} missing) — API only")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
