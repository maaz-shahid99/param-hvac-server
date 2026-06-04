"""
Central configuration — every value comes from the environment with a safe
local-dev default (see .env.example). Importing this module never fails and
never requires AWS or Postgres to be present.
"""

from __future__ import annotations

import os


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
