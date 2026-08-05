"""
Central configuration — every value comes from the environment with a safe
local-dev default (see .env.example). Importing this module never fails and
never requires AWS or Postgres to be present.
"""

from __future__ import annotations

import os


def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from a sibling .env file into the environment, WITHOUT
    overriding anything already set in the real environment (so a shell export /
    service manager / the test harness still wins). No external dependency — makes
    the appliance self-contained: `setup_appliance.py` writes .env and the server
    picks it up on the next start."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return
    # Keys this file has already set, so a LATER line in the file can override an
    # EARLIER one while a real environment variable still wins over the file
    # entirely. Without this the check below was first-wins within the file too:
    # appending `SMTP_HOST=smtp.gmail.com` to a file that already carried a blank
    # `SMTP_HOST=` from the template did nothing at all, silently, because the
    # blank line had already put the key in os.environ.
    from_file: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]                       # quoted -> take verbatim
        elif val.startswith("#"):
            # An EMPTY setting that carries an inline comment, e.g.
            #     SES_FROM=            # empty => fall through to SMTP
            # `val.strip()` above removes the leading spaces, so the " #" test
            # below never matched and the comment itself became the value. That
            # made blank-but-documented settings read as configured: SES_FROM
            # ended up truthy, so every alert attempted a doomed SES send before
            # falling through to the log.
            val = ""
        elif " #" in val:
            val = val.split(" #", 1)[0].strip()   # strip a trailing "  # comment"
        if key and (key not in os.environ or key in from_file):
            os.environ[key] = val
            from_file.add(key)


_load_dotenv()


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# --- Database --------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///cloud.db")

# --- Auth ------------------------------------------------------------------
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-insecure-change-me")
JWT_EXPIRE_HOURS = _i("JWT_EXPIRE_HOURS", 720)
JWT_ALG = "HS256"
BOOTSTRAP_TOKEN = os.environ.get("BOOTSTRAP_TOKEN", "dev-bootstrap")

# --- Manufacturer field-support access -------------------------------------
# A shared secret that lets the manufacturer's field-service console read fleet
# diagnostics (crashes/env/readings/health) and publish firmware over the LAN,
# WITHOUT a customer account. Blank => the whole /v1/support + OTA-publish API is
# DISABLED. Set a strong random value on the appliance to enable it.
SUPPORT_TOKEN = os.environ.get("SUPPORT_TOKEN", "")

# Where published firmware images + manifest.json live (served at /firmware/*).
FIRMWARE_DIR = os.path.abspath(
    os.environ.get("FIRMWARE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "firmware"))
)

# --- Discovery legacy root paths (deployed-firmware compatibility) ---------
# The discovery service is mounted at /discovery, but firmware provisioned BEFORE
# the merge appends its paths to the root of whatever URL it was given (e.g.
# POST /register/sensor, GET /discover) — which the standalone :8000 service used
# to serve. With this enabled the same routes are ALSO served at the root, so
# already-deployed gateways keep working with no reflash. Turn it off once every
# unit runs firmware that derives <cloud>/discovery.
DISCOVERY_LEGACY_ROOT = os.environ.get("DISCOVERY_LEGACY_ROOT", "1") not in ("", "0", "false", "False")

# --- mDNS / Bonjour discovery ----------------------------------------------
# Advertise a stable hostname on the LAN so the app/console/gateway can reach the
# appliance by name (http://<MDNS_NAME>.local:PORT) instead of a hard IP. Best-
# effort: needs the optional `zeroconf` package; a missing dep or failure is a
# no-op (connect by IP). Off by default in cloud/prod (no LAN).
MDNS_ENABLED = os.environ.get("MDNS_ENABLED", "1") not in ("", "0", "false", "False")
MDNS_NAME = os.environ.get("MDNS_NAME", "hvac-appliance")

# --- Password reset (email OTP) --------------------------------------------
OTP_TTL_S = _f("OTP_TTL_S", 600.0)          # reset code valid for 10 minutes
OTP_MAX_ATTEMPTS = _i("OTP_MAX_ATTEMPTS", 5)  # wrong-code tries before the code dies
MIN_PASSWORD_LEN = _i("MIN_PASSWORD_LEN", 6)

# --- Alerting --------------------------------------------------------------
DEFAULT_HIGH_C = _f("DEFAULT_HIGH_C", 40.0)
DEFAULT_DELTA_C = _f("DEFAULT_DELTA_C", 20.0)
HYSTERESIS_C = _f("HYSTERESIS_C", 3.0)
ALERT_COOLDOWN_S = _f("ALERT_COOLDOWN_S", 900.0)
STALE_AFTER_S = _f("STALE_AFTER_S", 180.0)
WATCHDOG_INTERVAL_S = _f("WATCHDOG_INTERVAL_S", 30.0)

# --- AWS notifications -----------------------------------------------------
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
SES_FROM = os.environ.get("SES_FROM", "")
SNS_SMS_ENABLED = os.environ.get("SNS_SMS_ENABLED", "0") not in ("", "0", "false", "False")

# --- SMTP email (local / on-prem alternative to SES) -----------------------
# Set SMTP_HOST to send OTP + alert email without AWS (Gmail app-password,
# Office 365, or a LAN relay). Leave empty to fall back to logging. Email
# delivery order is: SES (if SES_FROM set) -> SMTP (if SMTP_HOST set) -> log.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = _i("SMTP_PORT", 587)
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_STARTTLS = os.environ.get("SMTP_STARTTLS", "1") not in ("", "0", "false", "False")
MAIL_FROM = (os.environ.get("MAIL_FROM", "") or SES_FROM or SMTP_USER or "alerts@example.com")

# --- Twilio SMS (local / on-prem alternative to SNS) -----------------------
# Set the SID/token/from-number to send real SMS without AWS. SMS delivery
# order is: SNS (if SNS_SMS_ENABLED) -> Twilio (if SID set) -> log.
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM", "")     # your Twilio number, E.164

# --- Hardening -------------------------------------------------------------
# ENV selects the startup security posture:
#   dev (default) — no checks, frictionless local development.
#   onprem        — a LAN appliance (PC / node at the rack): enforce strong
#                   secrets, but ALLOW SQLite + HTTP + CORS '*' (a native app
#                   on a trusted LAN, no browser/RDS).
#   production    — cloud/AWS: strong secrets AND Postgres AND a CORS allowlist.
ENV = os.environ.get("ENV", "dev").lower()
IS_PROD = ENV in ("prod", "production")
IS_ONPREM = ENV in ("onprem", "on-prem", "local")

# Allowed CORS origins (comma-separated). Default "*" is fine for local dev but
# is REJECTED in production by validate_production() — set an explicit allowlist.
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]

# Per-IP sliding-window rate limit on the auth endpoints (login/register/forgot/
# reset). In-memory per process; behind many workers use a shared store (Redis).
AUTH_RATE_MAX = _i("AUTH_RATE_MAX", 10)
AUTH_RATE_WINDOW_S = _f("AUTH_RATE_WINDOW_S", 60.0)

_INSECURE_JWT = "dev-insecure-change-me"
_INSECURE_BOOTSTRAP = "dev-bootstrap"


def validate_startup() -> None:
    """For onprem/production, refuse to start with dev placeholders. No-op in
    dev so local work stays frictionless. onprem enforces only the checks that
    make sense on a trusted LAN; production additionally requires Postgres and a
    CORS allowlist."""
    if not (IS_PROD or IS_ONPREM):
        return
    problems = []
    if JWT_SECRET == _INSECURE_JWT or len(JWT_SECRET) < 32:
        problems.append("JWT_SECRET must be a strong random value (>= 32 chars)")
    if BOOTSTRAP_TOKEN == _INSECURE_BOOTSTRAP:
        problems.append("BOOTSTRAP_TOKEN must be changed (or set empty to disable registration)")
    if SUPPORT_TOKEN and len(SUPPORT_TOKEN) < 24:
        problems.append("SUPPORT_TOKEN must be a strong random value (>= 24 chars) or empty to disable")
    if IS_PROD:
        # Cloud-only: browser clients + a real database.
        if CORS_ORIGINS == ["*"]:
            problems.append("CORS_ORIGINS must be an explicit allowlist, not '*'")
        if DATABASE_URL.startswith("sqlite"):
            problems.append("DATABASE_URL must point at Postgres (not SQLite) in production")
    if problems:
        raise RuntimeError(
            f"Refusing to start ({ENV}): insecure config —\n  - " + "\n  - ".join(problems)
        )


# --- Server ----------------------------------------------------------------
PORT = _i("PORT", 8002)
