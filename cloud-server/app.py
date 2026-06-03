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
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

import config
from auth import (
    Principal,
    current_principal,
    generate_api_key,
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
    PasswordReset,
    Reading,
    SensorMap,
    SessionLocal,
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
    """(emails, phones) for a tenant. Explicit targets win; else all user emails."""
    t = db.get(Tenant, tenant_id)
    emails = [e.strip() for e in (t.alert_emails or "").split(",") if e.strip()] if t else []
    phones = [p.strip() for p in (t.alert_phones or "").split(",") if p.strip()] if t else []
    if not emails:
        emails = list(db.scalars(select(User.email).where(User.tenant_id == tenant_id)).all())
    return emails, phones


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

async def stale_watchdog() -> None:
    while True:
        await asyncio.sleep(config.WATCHDOG_INTERVAL_S)
        try:
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
    init_db()
    task = asyncio.create_task(stale_watchdog())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="HVAC Cloud Server", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# ---- auth ------------------------------------------------------------------ #

class RegisterBody(BaseModel):
    bootstrap_token: str
    tenant_name: str
    email: str
    password: str


class LoginBody(BaseModel):
    email: str
    password: str


@app.post("/v1/auth/register")
def register(body: RegisterBody, db: Session = Depends(get_db)):
    """Bootstrap a new tenant + its first admin user. Guarded by BOOTSTRAP_TOKEN."""
    if not config.BOOTSTRAP_TOKEN or body.bootstrap_token != config.BOOTSTRAP_TOKEN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bad bootstrap token")
    if db.scalar(select(User).where(User.email == body.email.lower())):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    tenant = Tenant(id=new_id(), name=body.tenant_name)
    db.add(tenant)
    user = User(id=new_id(), tenant_id=tenant.id, email=body.email.lower(),
                password_hash=hash_password(body.password), role="admin")
    db.add(user)
    db.commit()
    return {"token": issue_token(user), "tenant_id": tenant.id, "role": user.role}


@app.post("/v1/auth/login")
def login(body: LoginBody, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    return {"token": issue_token(user), "tenant_id": user.tenant_id, "role": user.role}


# ---- password reset (email OTP) -------------------------------------------- #

class ForgotBody(BaseModel):
    email: str


class ResetBody(BaseModel):
    email: str
    otp: str
    new_password: str


@app.post("/v1/auth/forgot")
def forgot_password(body: ForgotBody, db: Session = Depends(get_db)):
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
def reset_password(body: ResetBody, db: Session = Depends(get_db)):
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
