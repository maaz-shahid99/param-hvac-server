"""
Database layer — SQLAlchemy 2.0 ORM.

Works against SQLite (local dev, the default) and Postgres on AWS RDS, selected
purely by DATABASE_URL. Every business table carries a `tenant_id` so the data
is isolated per customer from day one, even while there is only one customer.
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    sessionmaker,
)

from config import DATABASE_URL

# SQLite needs check_same_thread off because the watchdog task and request
# handlers share the engine; Postgres ignores the arg.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> float:
    return time.time()


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    # Short shareable code members enter to request joining this org.
    org_code: Mapped[str] = mapped_column(String(16), default="", index=True)
    # Alert granularity: 'sensor' = one alert per sensor on its hottest probe
    # (legacy); 'probe' = each mapped probe alerts independently at its own
    # exhaust. Operator-selectable; default preserves the original behavior.
    alert_granularity: Mapped[str] = mapped_column(String(10), default="sensor")
    # How often (seconds) devices sample/forward env + sensor data. Operator-set;
    # propagated to the fleet via the gateway config-sync. Default 60s.
    collect_interval_s: Mapped[int] = mapped_column(Integer, default=60)
    # OPTIONAL extra external alert targets (comma-separated), in addition to the
    # per-member opt-ins. Usually empty — recipients come from member flags.
    alert_emails: Mapped[str] = mapped_column(String(2000), default="")
    alert_phones: Mapped[str] = mapped_column(String(2000), default="")
    created_at: Mapped[float] = mapped_column(Float, default=now)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    phone: Mapped[str] = mapped_column(String(32), default="")        # E.164 for SMS
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="member")   # admin|member
    # Join-request lifecycle: a new registrant is 'pending' until an admin
    # approves; only 'active' members receive notifications.
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|active|rejected
    # Admin-controlled per-member notification delivery.
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sms_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class PasswordReset(Base):
    """A pending email-OTP password reset. One active row per email (any prior
    row is deleted when a new code is requested). The code itself is never
    stored — only its SHA-256 hash — and it expires after config.OTP_TTL_S.
    `attempts` caps wrong-code guesses."""
    __tablename__ = "password_resets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), index=True)   # lower-case
    otp_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[float] = mapped_column(Float, default=0.0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class ApiKey(Base):
    """Per-site key the gateway uses to POST readings. We store only a SHA-256
    hash of the key; the raw value is shown once at creation time."""
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[float] = mapped_column(Float, default=now)
    last_used_at: Mapped[float] = mapped_column(Float, default=0.0)


class Topology(Base):
    """The app's full Rack -> Unit -> Port document, one per tenant. Mirrors the
    `rack_topology_json` the app used to keep only in SharedPreferences."""
    __tablename__ = "topologies"
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), primary_key=True)
    json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class SensorMap(Base):
    """Flattened (EUI, probe) -> physical location, derived from the topology on
    save so the ingest hot path is a single indexed lookup (no JSON parsing per
    reading). One sensor's probes can fan out to many ports, so the row is keyed
    by (eui, probe_rom): a given physical probe lives in exactly one port."""
    __tablename__ = "sensor_map"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    eui: Mapped[str] = mapped_column(String(32), index=True)         # lower-case hex
    # DS18B20 ROM (64-bit serial, 16-hex) of the probe wired to this port. "" =
    # whole-sensor legacy mapping (alerts/displays on the sensor's hottest probe).
    probe_rom: Mapped[str] = mapped_column(String(32), default="", index=True)
    box: Mapped[int] = mapped_column(Integer, default=0)
    slot: Mapped[str] = mapped_column(String(2), default="A")        # A=intake, B=exhaust
    label: Mapped[str] = mapped_column(String(300), default="")      # "Rack / Unit / Port"
    rack_id: Mapped[str] = mapped_column(String(64), default="")
    unit_id: Mapped[str] = mapped_column(String(64), default="")     # for intake/exhaust ΔT pairing
    port_id: Mapped[str] = mapped_column(String(64), default="")
    __table_args__ = (Index("ix_sensormap_tenant_eui_probe", "tenant_id", "eui", "probe_rom", unique=True),)


class Threshold(Base):
    """A high-temp / delta limit. scope='tenant' is the default; 'rack' or 'port'
    overrides for a specific rack_id/port_id (resolution order port > rack > tenant)."""
    __tablename__ = "thresholds"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    scope: Mapped[str] = mapped_column(String(10), default="tenant")  # tenant|rack|port
    scope_id: Mapped[str] = mapped_column(String(64), default="")     # rack_id or port_id
    high_c: Mapped[float] = mapped_column(Float)
    delta_c: Mapped[float] = mapped_column(Float)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (Index("ix_threshold_scope", "tenant_id", "scope", "scope_id", unique=True),)


class Reading(Base):
    __tablename__ = "readings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    ts: Mapped[float] = mapped_column(Float, index=True)
    eui: Mapped[str] = mapped_column(String(32), index=True)
    box: Mapped[int] = mapped_column(Integer, default=0)
    slot: Mapped[str] = mapped_column(String(2), default="A")
    probes: Mapped[str] = mapped_column(Text, default="[]")   # JSON list of {"rom": str, "c": float|null}
    max_c: Mapped[float] = mapped_column(Float, default=0.0)  # hottest valid probe
    __table_args__ = (Index("ix_reading_tenant_ts", "tenant_id", "ts"),)


class EnvReading(Base):
    """A router/gateway environmental sample (BME280/680): temperature, humidity,
    pressure, VOC/gas. Routers have no Wi-Fi, so these reach the cloud over the
    Thread mesh via the active gateway. High-volume time series, keyed like
    Reading so the live tab + CSV export are cheap, indexed scans."""
    __tablename__ = "env_readings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    ts: Mapped[float] = mapped_column(Float, index=True)
    eui: Mapped[str] = mapped_column(String(32), index=True)
    temp: Mapped[float] = mapped_column(Float, default=0.0)
    hum: Mapped[float] = mapped_column(Float, default=0.0)
    pres: Mapped[float] = mapped_column(Float, default=0.0)
    voc: Mapped[float] = mapped_column(Float, default=0.0)
    __table_args__ = (Index("ix_envreading_tenant_ts", "tenant_id", "ts"),)


class MeshNode(Base):
    """A non-sensor Thread mesh device (a router) the gateway reported via
    /v1/mesh. Routers don't send readings, so this is the only record of their
    existence + liveness; `online` is derived from `last_seen` freshness."""
    __tablename__ = "mesh_nodes"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    eui: Mapped[str] = mapped_column(String(32), index=True)          # lower-case hex
    kind: Mapped[str] = mapped_column(String(16), default="router")
    last_seen: Mapped[float] = mapped_column(Float, default=now)
    __table_args__ = (Index("ix_meshnode_tenant_eui", "tenant_id", "eui", unique=True),)


class CommissionedDevice(Base):
    """Per-tenant registry of commissioned devices (membership + type), so the
    app's Devices list survives phone changes / reinstalls instead of living only
    in one phone's local cache. One row per (tenant, eui). Online status is still
    computed client-side from readings/mesh/BLE — this is just the roster."""
    __tablename__ = "commissioned_devices"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    eui: Mapped[str] = mapped_column(String(32), index=True)          # lower-case hex
    kind: Mapped[str] = mapped_column(String(16), default="sensor")   # sensor|router|gateway
    role: Mapped[str] = mapped_column(String(2), default="")          # G|R for mesh nodes
    # Operator-assigned friendly name. "" => the app shows an EUI-derived auto-name.
    name: Mapped[str] = mapped_column(String(120), default="")
    added_at: Mapped[float] = mapped_column(Float, default=now)
    __table_args__ = (Index("ix_commdev_tenant_eui", "tenant_id", "eui", unique=True),)


class CrashReport(Base):
    """A firmware panic report forwarded by a device — the gateway directly, a
    router over the mesh. reset_reason + pc + backtrace come from the ESP
    core-dump summary; `detail` carries extra text (recent log lines). Kept so an
    operator can see + download fleet crashes without a serial cable."""
    __tablename__ = "crash_reports"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    eui: Mapped[str] = mapped_column(String(32), index=True)
    ts: Mapped[float] = mapped_column(Float, index=True, default=now)
    reset_reason: Mapped[str] = mapped_column(String(40), default="")
    fw: Mapped[str] = mapped_column(String(40), default="")
    pc: Mapped[str] = mapped_column(String(20), default="")
    backtrace: Mapped[str] = mapped_column(Text, default="")   # space-separated hex addrs
    detail: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (Index("ix_crash_tenant_ts", "tenant_id", "ts"),)


class Alert(Base):
    """An open or historical alert. One open alert per (tenant, eui, kind)."""
    __tablename__ = "alerts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    eui: Mapped[str] = mapped_column(String(32), index=True)
    location: Mapped[str] = mapped_column(String(300), default="")
    kind: Mapped[str] = mapped_column(String(20))            # high_temp|delta|stale
    state: Mapped[str] = mapped_column(String(20), default="open")  # open|acked|cleared
    value: Mapped[float] = mapped_column(Float, default=0.0)
    threshold: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[float] = mapped_column(Float, default=now)
    cleared_at: Mapped[float] = mapped_column(Float, default=0.0)
    last_notified_at: Mapped[float] = mapped_column(Float, default=0.0)
    __table_args__ = (Index("ix_alert_open", "tenant_id", "eui", "kind", "state"),)


class FirmwareRelease(Base):
    """A firmware image the manufacturer published for the fleet to OTA onto.
    `kind` picks the chip (c3/c6); `severity` drives rollout — 'mandatory' is
    auto-applied fleet-wide by the gateway's OTA poll, 'optional' waits for an
    in-app approval. The highest `version` per kind is the current target. The
    .bin lives on disk under config.FIRMWARE_DIR and is served at /firmware/<file>."""
    __tablename__ = "firmware_releases"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(4), index=True)          # c3|c6
    version: Mapped[int] = mapped_column(Integer, index=True)
    severity: Mapped[str] = mapped_column(String(10), default="optional")  # mandatory|optional
    # Rollout stage: 'canary' = only the gateway self-updates (verify-first);
    # 'full' = the gateway broadcasts to the whole fleet. Promote canary->full
    # once the gateway reports the new version healthy.
    stage: Mapped[str] = mapped_column(String(10), default="full")    # canary|full
    filename: Mapped[str] = mapped_column(String(200), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=now, index=True)


class OtaState(Base):
    """Per-tenant approval of an OPTIONAL firmware version. A customer admin
    approves a version in the app; the gateway applies it on its next OTA poll.
    Mandatory updates ignore this (always auto-applied)."""
    __tablename__ = "ota_state"
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), primary_key=True)
    approved_c3: Mapped[int] = mapped_column(Integer, default=0)
    approved_c6: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class FleetStatus(Base):
    """The active gateway's self-reported status (firmware versions, heap, role),
    upserted from the /v1/mesh roster post. Drives the support console's fleet
    health and the OTA 'is there a newer version' comparison."""
    __tablename__ = "fleet_status"
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), primary_key=True)
    fw_c3: Mapped[int] = mapped_column(Integer, default=0)
    fw_c6: Mapped[int] = mapped_column(Integer, default=0)
    heap_free: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(16), default="")
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class SupportAudit(Base):
    """Append-only log of manufacturer support-token access — every read + every
    firmware publish — so a customer admin can see when their appliance was
    serviced and what was touched."""
    __tablename__ = "support_audit"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ts: Mapped[float] = mapped_column(Float, default=now, index=True)
    action: Mapped[str] = mapped_column(String(60), default="")
    detail: Mapped[str] = mapped_column(String(500), default="")


class SingletonLease(Base):
    """A short-lived leader lease so exactly one process runs a singleton job
    (the stale-sensor watchdog) even when the API is scaled to many workers.
    The holder renews before `expires_at`; if it dies, another worker takes over
    once the lease lapses. Portable across SQLite (dev) and Postgres (prod)."""
    __tablename__ = "singleton_leases"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    holder: Mapped[str] = mapped_column(String(64), default="")
    expires_at: Mapped[float] = mapped_column(Float, default=0.0)


def init_db() -> None:
    """Bootstrap the schema for local dev / tests on SQLite. On a real database
    (Postgres) the schema is owned by Alembic — run `alembic upgrade head`
    before starting the app — so we DON'T create_all there (it would race with
    and confuse migrations)."""
    if DATABASE_URL.startswith("sqlite"):
        Base.metadata.create_all(engine)
