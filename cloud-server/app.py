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
import json
import os
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import config
from auth import (
    Principal,
    current_principal,
    generate_api_key,
    generate_org_code,
    generate_otp,
    get_db,
    hash_otp,
    hash_password,
    issue_token,
    require_admin,
    tenant_from_api_key,
    verify_password,
)
from db import (
    Alert,
    ApiKey,
    MeshNode,
    PasswordReset,
    Reading,
    SensorMap,
    SessionLocal,
    SingletonLease,
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

def parse_probe_csv(data: str) -> list[float | None]:
    """'t=23.1,24.0,err' (or a bare CSV) -> [23.1, 24.0, None]."""
    if "=" in data:
        data = data.split("=", 1)[1]
    out: list[float | None] = []
    for tok in data.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.lower() == "err":
            out.append(None)
        else:
            try:
                out.append(float(tok))
            except ValueError:
                out.append(None)
    return out


def hottest(probes: list[float | None]) -> float:
    vals = [p for p in probes if isinstance(p, (int, float))]
    return max(vals) if vals else 0.0


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
    for rack in topo.get("racks", []):
        r_name = rack.get("name", "")
        for unit in rack.get("units", []):
            u_name = unit.get("name", "")
            for port in unit.get("ports", []):
                eui = (port.get("assignedEui") or "").strip().lower()
                if not eui:
                    continue
                slot = "B" if port.get("type") == "exhaust" else "A"
                label = " / ".join(p for p in (r_name, u_name, port.get("label", "")) if p)
                db.add(SensorMap(
                    id=new_id(), tenant_id=tenant_id, eui=eui,
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


def _scan_stale() -> None:
    cutoff = now() - config.STALE_AFTER_S
    with SessionLocal() as db:
        from thresholds import _open_alert  # local import avoids a cycle at import time
        for sm in db.scalars(select(SensorMap)).all():
            last = db.scalar(
                select(Reading).where(Reading.tenant_id == sm.tenant_id, Reading.eui == sm.eui)
                .order_by(Reading.ts.desc()).limit(1)
            )
            if last and last.ts < cutoff:
                loc = sm.label or f"sensor {sm.eui}"
                _open_alert(db, sm.tenant_id, recipients_for(db, sm.tenant_id),
                            sm.eui, "stale", loc, 0.0, 0.0)


# --------------------------------------------------------------------------- #
# App                                                                          #
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    config.validate_startup()   # fail fast on insecure onprem/prod config (no-op in dev)
    init_db()
    task = asyncio.create_task(stale_watchdog())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="HVAC Cloud Server", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"], allow_headers=["*"],
)


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
def join(body: JoinBody, db: Session = Depends(get_db),
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


@app.post("/v1/members/{member_id}/approve")
def approve_member(member_id: str, p: Principal = Depends(require_admin),
                   db: Session = Depends(get_db)):
    u = _get_member(db, p, member_id)
    u.status = "active"
    db.commit()
    return {"ok": True, "member": _member_dict(u)}


@app.post("/v1/members/{member_id}/reject")
def reject_member(member_id: str, p: Principal = Depends(require_admin),
                  db: Session = Depends(get_db)):
    u = _get_member(db, p, member_id)
    if u.id == p.user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't reject yourself")
    u.status = "rejected"
    u.email_enabled = False
    u.sms_enabled = False
    db.commit()
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
        u.role = body.role
    db.commit()
    return {"ok": True, "member": _member_dict(u)}


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
    rows = db.scalars(select(ApiKey).where(ApiKey.tenant_id == p.tenant_id)).all()
    return {"keys": [{"label": k.label, "created_at": k.created_at,
                      "last_used_at": k.last_used_at} for k in rows]}


# ---- ingest (gateway -> cloud) --------------------------------------------- #

class IngestBody(BaseModel):
    sensor_id: str                                   # EUI-64 hex
    probes: list[float | None] = Field(default_factory=list)
    data: str | None = None                          # raw "t=23.1,24.0,err" form
    ts: float | None = None


@app.post("/v1/readings")
def ingest(body: IngestBody, tenant_id: str = Depends(tenant_from_api_key),
           db: Session = Depends(get_db)):
    """Gateway posts a reading. Authenticated by X-API-Key -> tenant."""
    probes = body.probes
    if not probes and body.data:
        probes = parse_probe_csv(body.data)
    eui = body.sensor_id.strip().lower()
    ts = body.ts or now()
    mx = hottest(probes)

    sm = db.scalar(select(SensorMap).where(SensorMap.tenant_id == tenant_id, SensorMap.eui == eui))
    db.add(Reading(
        tenant_id=tenant_id, ts=ts, eui=eui,
        box=(sm.box if sm else 0), slot=(sm.slot if sm else "A"),
        probes=json.dumps(probes), max_c=mx,
    ))
    db.commit()

    # A fresh reading clears any open "stale" alert for this sensor.
    _clear_alert(db, tenant_id, eui, "stale")
    evaluate_reading(db, tenant_id, eui, mx, recipients_for(db, tenant_id))
    return {"ok": True, "eui": eui, "max_c": mx}


# ---- mesh roster (routers) ------------------------------------------------- #

class MeshBody(BaseModel):
    nodes: list[dict] = []     # [{"eui": "...", "role": "G|R"}]
    routers: list[dict] = []   # legacy: [{"eui": "..."}] (treated as routers)


@app.post("/v1/mesh")
def ingest_mesh(body: MeshBody, tenant_id: str = Depends(tenant_from_api_key),
                db: Session = Depends(get_db)):
    """Gateway posts the live C6 mesh roster (gateway + routers — these have no
    readings of their own). Upserts each as a MeshNode with a fresh last_seen +
    kind; the dashboard derives online/offline from the timestamp, and the
    'gateway' kind moves on failover."""
    ts = now()
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
    """Latest reading per sensor for the tenant (for the app's status view)."""
    euis = db.scalars(select(Reading.eui).where(Reading.tenant_id == p.tenant_id).distinct()).all()
    out = []
    for eui in euis:
        r = db.scalar(select(Reading).where(Reading.tenant_id == p.tenant_id, Reading.eui == eui)
                      .order_by(Reading.ts.desc()).limit(1))
        sm = db.scalar(select(SensorMap).where(SensorMap.tenant_id == p.tenant_id, SensorMap.eui == eui))
        out.append({"eui": eui, "ts": r.ts, "max_c": r.max_c,
                    "probes": json.loads(r.probes),
                    "location": (sm.label if sm else ""), "box": r.box, "slot": r.slot})
    return {"sensors": out}


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


@app.put("/v1/recipients")
def set_recipients(body: RecipientsBody, p: Principal = Depends(require_admin),
                   db: Session = Depends(get_db)):
    t = db.get(Tenant, p.tenant_id)
    t.alert_emails, t.alert_phones = body.alert_emails, body.alert_phones
    db.commit()
    return {"ok": True}


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
_API_PREFIXES = ("v1/", "v1", "health", "docs", "redoc", "openapi.json")

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
