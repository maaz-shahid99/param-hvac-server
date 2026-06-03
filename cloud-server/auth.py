"""
Authentication & authorization helpers.

Two independent credentials:
  - Users (the app)   -> email/password, verified with bcrypt, carry a JWT.
  - Gateways (ingest) -> a per-site API key, sent as "X-API-Key", resolved to a
    tenant by SHA-256 hash lookup.

Both resolve a request down to a tenant_id so handlers never see another
customer's data.
"""

from __future__ import annotations

import hashlib
import secrets
import time

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

import config
from db import ApiKey, SessionLocal, User


# --- DB session dependency -------------------------------------------------

def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- Password hashing ------------------------------------------------------

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


# --- API keys --------------------------------------------------------------

def generate_api_key() -> tuple[str, str]:
    """Return (raw_key, sha256_hash). The raw key is shown to the operator once;
    only the hash is stored."""
    raw = "hvac_" + secrets.token_urlsafe(32)
    return raw, hash_api_key(raw)


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# --- One-time password (email reset code) ----------------------------------

def generate_otp() -> str:
    """A 6-digit numeric reset code, e.g. '042517'."""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_otp(code: str) -> str:
    """Store only the hash, like API keys — never the plaintext code."""
    return hashlib.sha256(code.strip().encode()).hexdigest()


# --- JWT -------------------------------------------------------------------

def issue_token(user: User) -> str:
    payload = {
        "sub": user.id,
        "tid": user.tenant_id,
        "role": user.role,
        "exp": int(time.time()) + config.JWT_EXPIRE_HOURS * 3600,
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALG)


def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALG])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")


class Principal:
    """The authenticated user behind a request, scoped to one tenant."""

    def __init__(self, user_id: str, tenant_id: str, role: str):
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.role = role


def current_principal(authorization: str = Header(default="")) -> Principal:
    """Dependency for app endpoints: requires a valid bearer JWT."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    claims = _decode_token(authorization.split(" ", 1)[1].strip())
    return Principal(claims.get("sub", ""), claims.get("tid", ""), claims.get("role", "viewer"))


def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
    if principal.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return principal


def tenant_from_api_key(
    x_api_key: str = Header(default=""),
    db: Session = Depends(get_db),
) -> str:
    """Dependency for the gateway ingest endpoint: resolves X-API-Key -> tenant_id."""
    if not x_api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing X-API-Key")
    row = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(x_api_key)))
    if not row:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
    row.last_used_at = time.time()
    db.commit()
    return row.tenant_id
