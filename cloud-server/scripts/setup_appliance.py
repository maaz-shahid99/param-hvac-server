#!/usr/bin/env python3
"""
Appliance setup — generate strong secrets and write a hardened .env so the box
refuses to boot with insecure dev defaults (config.validate_startup() enforces
this once ENV is onprem/production; config._load_dotenv() picks the file up).

Idempotent-safe: it will NOT overwrite an existing .env unless you pass --force,
because rotating JWT_SECRET invalidates every signed-in session.

Usage (from the Cloud Server dir):
    python scripts/setup_appliance.py               # write .env for an onprem box
    python scripts/setup_appliance.py --force        # regenerate (ROTATES secrets!)
    python scripts/setup_appliance.py --env production
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                 # the Cloud Server directory
ENV_PATH = os.path.join(ROOT, ".env")
EXAMPLE_PATH = os.path.join(ROOT, ".env.example")


def _set_kv(lines: list[str], key: str, value: str) -> list[str]:
    """Replace an uncommented KEY=... line in place; append if absent."""
    out, done = [], False
    for ln in lines:
        s = ln.lstrip()
        if s and not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(ln)
    if not done:
        out.append(f"{key}={value}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate secrets + write .env for an appliance.")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing .env (ROTATES secrets — logs everyone out)")
    ap.add_argument("--env", default="onprem", choices=["onprem", "production"],
                    help="hardening posture written to ENV (default: onprem)")
    args = ap.parse_args()

    if os.path.exists(ENV_PATH) and not args.force:
        print(f"Refusing to overwrite {ENV_PATH} (use --force to rotate secrets).")
        return 1
    if not os.path.exists(EXAMPLE_PATH):
        print(f"Missing template: {EXAMPLE_PATH}", file=sys.stderr)
        return 2

    with open(EXAMPLE_PATH, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    jwt_secret = secrets.token_urlsafe(48)    # ~64 chars, well over the 32 floor
    bootstrap = secrets.token_urlsafe(24)
    lines = _set_kv(lines, "ENV", args.env)
    lines = _set_kv(lines, "JWT_SECRET", jwt_secret)
    lines = _set_kv(lines, "BOOTSTRAP_TOKEN", bootstrap)
    content = "\n".join(lines).rstrip("\n") + "\n"

    # Write with owner-only perms where the OS honours it (no-op on Windows ACLs).
    fd = os.open(ENV_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)

    # Confirm the result actually passes the boot-time check for this posture.
    os.environ["ENV"] = args.env
    os.environ["JWT_SECRET"] = jwt_secret
    os.environ["BOOTSTRAP_TOKEN"] = bootstrap
    sys.path.insert(0, ROOT)
    import config  # noqa: E402  (reads the env we just set)
    passes = True
    try:
        config.validate_startup()
    except RuntimeError as e:
        passes = False
        validate_msg = str(e)

    print(f"\nWrote {ENV_PATH}  (ENV={args.env})")
    print("Generated a strong JWT_SECRET and BOOTSTRAP_TOKEN.")
    print(f"\n    BOOTSTRAP_TOKEN = {bootstrap}\n")
    print("Use that token ONCE to register the first admin org (app or web), then")
    print("set BOOTSTRAP_TOKEN= (empty) in .env to disable further self-registration.")
    if args.env == "production":
        print("\nproduction posture also REQUIRES (fill these in .env or the box won't start):")
        print("  - DATABASE_URL=postgresql+psycopg://USER:PASS@host:5432/hvac")
        print("  - CORS_ORIGINS=https://your-dashboard-host   (no '*')")
    print("\nNext steps:")
    print("  pip install -r requirements.txt")
    print("  alembic upgrade head            # apply DB migrations")
    print("  python -m uvicorn app:app --host 0.0.0.0 --port 8002")
    if not passes:
        print(f"\nNOTE: validate_startup still reports (expected until you fill the above):\n  {validate_msg}")
    else:
        print("\nvalidate_startup() passes — the server will boot with this config.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
