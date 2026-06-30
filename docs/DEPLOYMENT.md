# Deployment / production-readiness checklist

Going from the LAN dev rig to something a paying customer relies on. Each item
notes the **current state in this repo** and what to change. Priorities:
🔴 = blocker before first customer · 🟡 = soon after first install.

> **Deployment model assumed:** Cloud Server hosted by the vendor in **AWS**
> (multi-tenant SaaS); the `Discovery Server/display_node.py` LAN dashboard is
> optional on-site. The discovery server is single-site rendezvous only.
>
> **Running locally / on-prem instead** (on a PC or a node at the rack, no AWS)?
> Most of §1–§3 below does not apply — see **`Cloud Server/deploy/LOCAL_SETUP.md`**.
> Use `ENV=onprem` (SQLite + HTTP allowed, strong secrets enforced) and SMTP for
> email.

---

## 0. Security hardening (see `SECURITY.md` for details)
- [x] 🔴 Firmware: **`security.c` HMAC enforced** — unsigned/mismatched commands now rejected (constant-time); `add` is end-to-end app-signed.
- [x] 🔴 Firmware: **per-device PSKd provisionable** — sensor loads its PSKd from NVS (`factory/pskd`) with a dev fallback. *Remaining ops:* write a unique PSKd per unit at the factory + print it on the QR (process in `SECURITY.md §2`).
- [x] 🟡 Firmware: **credential/PIN/SSID logging gated** behind `LOG_SENSITIVE` (default 0). *NVS-at-rest encryption:* enablement steps in `SECURITY.md §4` (irreversible eFuse step — do at manufacturing).
- [x] 🔴 Cloud: **fail-fast on insecure config** — with `ENV=production` the server refuses to start on default `JWT_SECRET`/`BOOTSTRAP_TOKEN`/`CORS=*`/SQLite (validated for `>=24` char `SUPPORT_TOKEN` too). *Remaining ops:* set the real secret values — `JWT_SECRET`, `BOOTSTRAP_TOKEN`, and (to enable field-service) `SUPPORT_TOKEN`. See [.env.example](Cloud%20Server/.env.example).
- [x] 🟡 App: **`allowBackup="false"`** + backup/extraction rules exclude all app data (JWT can't survive reinstall/cloud restore).

---

## 1. Cloud Server (the product — biggest gap)
- [x] 🔴 **Postgres-ready** — point `DATABASE_URL` at **AWS RDS Postgres** (`psycopg` in requirements). Schema is now Alembic-managed; `init_db` only auto-creates on SQLite. *Remaining ops:* provision RDS + run `alembic upgrade head` (see `Cloud Server/deploy/AWS_SETUP.md`).
- [~] 🔴 **HTTPS/TLS.** Code side **done**: the C3 gateway now speaks TLS to `https://` cloud URLs (`WiFiClientSecure`, `Bridge.ino`), and `Cloud Server/deploy/{nginx.conf,hvac-cloud.service}` terminate TLS in front of uvicorn. *Remaining ops:* domain + cert (certbot/ACM).
- [ ] 🔴 **SES out of sandbox + verified sender domain** with **SPF/DKIM/DMARC**. Today alerts/OTP only *log* (`notifications.py` fallback). Code path is correct — this is an AWS-account action. Step-by-step in `Cloud Server/deploy/AWS_SETUP.md`. Set `SES_FROM`; for SMS set `SNS_SMS_ENABLED=1`.
- [x] 🔴 **Watchdog is single-instance-safe.** `stale_watchdog` now claims a DB **leader lease** (`SingletonLease`); only one worker/instance runs the scan, the rest stand by — no duplicate alerts when scaled.
- [x] 🔴 **Real migrations (Alembic).** `migrations/` + initial schema; `alembic upgrade head` on deploy, `alembic check` in CI. `init_db` no longer `create_all`s on Postgres.
- [x] 🟡 **CORS lockdown** — `CORS_ORIGINS` env allowlist (prod rejects `*`).
- [x] 🟡 **Rate-limiting** — per-IP sliding window on `login`/`register`/`forgot`/`reset` (`AUTH_RATE_MAX`/`AUTH_RATE_WINDOW_S`). At scale, back it with Redis.
- [ ] 🟡 **Readings retention/rollup.** `readings` grows unbounded — prune/downsample (or TimescaleDB) for cost + query speed.
- [ ] 🟡 **Observability + durability:** structured logs, error tracking (Sentry), `/health` wired to the LB, CloudWatch metrics/alarms, RDS automated backups.
- [ ] 🟡 **Process management:** Docker image + ECS/EC2 with a process manager; multiple workers behind the LB (only after the watchdog fix).

## 2. Flutter app (productization)
- [ ] 🔴 **App identity:** change the default `applicationId`/bundle ID (still `com.example.thread_commissioner`?), app name/label, icon, splash.
- [ ] 🔴 **Release signing + distribution:** Android release keystore (signed AAB) / iOS provisioning; decide Play Store / App Store / internal MDM.
- [ ] 🔴 **Remove cleartext + bake prod URL.** Drop `usesCleartextTraffic="true"` once cloud is HTTPS; ship a **default production cloud URL** so customers don't type an IP.
- [ ] 🔴 **Hide the bootstrap / "Set up a new organization" flow** from end users — tenant creation is a vendor action; customers only **log in / reset password**.
- [ ] 🟡 App **versioning** (pubspec version + build numbers) tied to releases.

## 3. Firmware / gateway
- [ ] 🔴 **Production URLs + TLS on the C3.** `Bridge.ino` bakes a dev IP (`DEFAULT_DISCOVERY_URL "http://10.14.98.109:8000"`, empty `DEFAULT_CLOUD_URL`) over plain HTTP. HTTPS cloud requires `WiFiClientSecure` + CA cert/pinning — a real change, not just a string swap.
- [x] 🟡 **Cloud firmware/OTA hosting** — the Cloud Server now hosts `/firmware/manifest.json` + images and orchestrates **tiered OTA**: the gateway polls `/v1/ota/check` (v19+) and self-updates; mandatory auto-rolls, optional waits for in-app approval, **canary → promote** rolls the gateway first. Publish from the **field-console**. *Remaining ops:* per-device identity + QR generation + the factory-flash procedure.
- [ ] 🟡 **Radio regulatory certification** (FCC/CE for 802.15.4) + enclosure/power if selling the hardware.

## 3b. Field-service plane + firmware OTA (manufacturer)
- [x] 🔴 **Support API gated by `SUPPORT_TOKEN`** — `/v1/support/*` (cross-tenant diagnostics) + firmware publish are **disabled (404) unless `SUPPORT_TOKEN` is set**. *Remaining ops:* set a strong token (≥24 chars) on appliances you'll service; it's a powerful secret — keep it on a trusted LAN or behind HTTPS.
- [x] 🟡 **field-console** (`field-console/`, React) — manufacturer LAN tool: fleet health, crashes (+addr2line helper), env/readings, alerts, firmware publish + canary/promote. `npm run build`; keep internal.
- [x] 🟡 **Support access is audit-logged** (`support_audit`) and visible to the customer admin (`GET /v1/support-access`).
- [x] 🟡 **mDNS discovery** — the appliance advertises `hvac-appliance.local` (optional `zeroconf`; fails open). Add `zeroconf` to the appliance venv to enable.
- [ ] 🟡 **Firmware image signing** — images are SHA-256 + version-gated but not signed. Asymmetric signing is a hardening follow-up.

## 4. Onboarding & operations
- [ ] 🔴 **Customer provisioning runbook/tool:** create tenant + first admin, deliver credentials, mint the gateway API key. Today it's manual `curl` + the app admin menu.
- [ ] 🟡 **Staging environment + CI:** run `Cloud Server/test_local.py` and `flutter analyze` on every push; documented deploy.
- [ ] 🟡 **Legal/privacy:** storing customer emails/phones + temperature data → privacy policy, retention policy, GDPR/contract terms as applicable.

---

## True blockers (do these four first)
1. HTTPS/TLS end to end — **code done** (C3 TLS client + Nginx/systemd templates); remaining: domain + cert.
2. Postgres (RDS) instead of SQLite — **code/migrations done** (Alembic); remaining: provision RDS + `alembic upgrade head`.
3. SES out of sandbox + domain auth (SPF/DKIM/DMARC) — **AWS-account action**; runbook in `Cloud Server/deploy/AWS_SETUP.md`.
4. Single-instance watchdog — **done** (DB leader lease).

> The code-side of all four is complete. What's left (1–3) is AWS account/DNS
> setup, documented step-by-step in `Cloud Server/deploy/AWS_SETUP.md`.

## Quick reference — what's already production-shaped
- Multi-tenant data model (`tenant_id` on every table), JWT auth, hashed API keys & OTP codes.
- Threshold engine with hysteresis + cooldown; stale-sensor detection.
- Config is fully env-driven with safe defaults (`config.py` / `.env.example`) — prod is mostly setting env vars + the items above.
- **Fleet env logging + per-probe temps + firmware crash reporting** → cloud, with CSV export.
- **Manufacturer field-service plane** (`SUPPORT_TOKEN`-gated) + **field-console** + **tiered/canary OTA** + audit log + mDNS discovery.
- End-to-end smoke test (`test_local.py`) and the hardware bring-up guide (`FULL_SYSTEM_TEST.md`).

## Verified build state (this pass)
Cloud `test_local.py` ✅ · `alembic` single head `a1b8e6c2f9d4`, chain applies ✅ ·
web-dashboard + field-console `build` ✅ · `flutter analyze` 0 errors/warnings ✅ ·
C3 firmware compiles (**v19**) ✅ · C6 builds in ESP-IDF (**v16**, not in CI here).
