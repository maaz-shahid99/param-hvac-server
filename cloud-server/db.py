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
    """Flattened EUI -> physical location, derived from the topology on save so
    the ingest hot path is a single indexed lookup (no JSON parsing per reading)."""
    __tablename__ = "sensor_map"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(32), ForeignKey("tenants.id"), index=True)
    eui: Mapped[str] = mapped_column(String(32), index=True)         # lower-case hex
    box: Mapped[int] = mapped_column(Integer, default=0)
    slot: Mapped[str] = mapped_column(String(2), default="A")        # A=intake, B=exhaust
    label: Mapped[str] = mapped_column(String(300), default="")      # "Rack / Unit / Port"
    rack_id: Mapped[str] = mapped_column(String(64), default="")
    unit_id: Mapped[str] = mapped_column(String(64), default="")     # for intake/exhaust ΔT pairing
    port_id: Mapped[str] = mapped_column(String(64), default="")
    __table_args__ = (Index("ix_sensormap_tenant_eui", "tenant_id", "eui", unique=True),)


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
    probes: Mapped[str] = mapped_column(Text, default="[]")   # JSON list of float|null
    max_c: Mapped[float] = mapped_column(Float, default=0.0)  # hottest valid probe
    __table_args__ = (Index("ix_reading_tenant_ts", "tenant_id", "ts"),)


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
