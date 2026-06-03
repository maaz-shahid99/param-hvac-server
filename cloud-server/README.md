# Cloud Server — multi-tenant rack-overheat alerting

The product backend. Runs on AWS (alongside the discovery server), ingests
readings from gateways, evaluates them against per-customer thresholds, and
alerts the customer (SES email / SNS SMS) when a server rack or data node
overheats — or when a sensor goes silent.

The Flutter app (`thread_commissioner/`) is the control plane (log in, configure
the rack layout, set thresholds, view live temps and alerts); **this** service
is the always-on engine, because a phone is not always running.

## Why this exists / how it differs from the other servers
- `Discovery Server/discovery_server.py` — rendezvous + a presence-only "site
  offline" email. Single-site, no accounts. **Unchanged.**
- `Discovery Server/display_node.py` — LAN dashboard + time-series, **no
  threshold logic**, no tenants. **Unchanged** (the gateway still posts to it for
  the local 3D dashboard).
- **This Cloud Server** — multi-tenant, JWT auth, API-key ingest, the threshold
  engine, and SES/SNS alerts. Every table carries a `tenant_id`.

## Architecture (Phase 1)
```
Gateway (Bridge C3)  --HTTPS POST /v1/readings, X-API-Key-->  Cloud Server
                                                                 |
                                          threshold engine (hysteresis+cooldown)
                                                                 |
                                                   SES email + SNS SMS  -> customer
App (Flutter) <--JWT--> /v1/auth, /v1/topology, /v1/thresholds, /v1/current, /v1/alerts
```

## Run locally (no AWS, no Postgres)
Everything defaults to SQLite + log-only notifications, so it runs as-is:
```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8002
```
Smoke-test the whole alert loop:
```bash
python test_local.py        # register -> ingest hot reading -> alert -> clear
```

## Deploy on AWS (Phase 1 checklist — needs your AWS account)
1. **RDS Postgres** → set `DATABASE_URL=postgresql+psycopg://USER:PASS@HOST:5432/hvac`.
2. **SES** → verify a sender, set `SES_FROM`. (Leave blank to keep logging.)
3. **SNS** → set `SNS_SMS_ENABLED=1` for SMS. Creds via the instance IAM role.
4. Set a strong `JWT_SECRET` and a real `BOOTSTRAP_TOKEN`.
5. Run behind HTTPS (ALB / Nginx) on the same host as the discovery server.
6. Bootstrap the first customer + gateway key:
   ```bash
   curl -X POST $URL/v1/auth/register -H 'content-type: application/json' \
     -d '{"bootstrap_token":"...","tenant_name":"Acme","email":"a@acme.com","password":"..."}'
   # then in the app (admin menu) → Generate gateway API key
   ```
7. Provision the gateway over BLE with the cloud URL + key (PROVISION payload
   `cloud` / `cloudKey`), see `Bridge/Bridge.ino`.

## Configuration
All env vars (with safe defaults) are documented in `.env.example`. Key ones:
`DATABASE_URL`, `JWT_SECRET`, `BOOTSTRAP_TOKEN`, `DEFAULT_HIGH_C`,
`DEFAULT_DELTA_C`, `HYSTERESIS_C`, `ALERT_COOLDOWN_S`, `STALE_AFTER_S`,
`SES_FROM`, `SNS_SMS_ENABLED`, `AWS_REGION`, `PORT`.

## API summary
| Method & path | Auth | Purpose |
|---|---|---|
| `POST /v1/auth/register` | bootstrap token | create tenant + admin |
| `POST /v1/auth/login` | — | email/password → JWT |
| `POST /v1/apikeys` | JWT (admin) | mint a gateway API key (shown once) |
| `POST /v1/readings` | X-API-Key | gateway ingest + threshold eval |
| `GET/PUT /v1/topology` | JWT | rack layout sync (replaces app-local) |
| `GET/PUT /v1/thresholds` | JWT (PUT=admin) | per-tenant/rack/port limits |
| `GET /v1/current` | JWT | latest temp per sensor |
| `GET /v1/alerts?state=` | JWT | open/all alerts |
| `POST /v1/alerts/{id}/ack` | JWT | acknowledge an alert |
| `PUT /v1/recipients` | JWT (admin) | alert email/phone targets |

## Files
- `app.py` — FastAPI app, endpoints, topology→sensor_map flattening, stale watchdog.
- `db.py` — SQLAlchemy models (all `tenant_id`-scoped), SQLite/Postgres via `DATABASE_URL`.
- `auth.py` — bcrypt passwords, JWT, API-key hashing, request→tenant dependencies.
- `thresholds.py` — high-temp + ΔT evaluation, alert lifecycle (hysteresis/cooldown).
- `notifications.py` — SES/SNS dispatch with log-only fallback.
- `config.py` — env-driven config with safe defaults.
- `test_local.py` — end-to-end smoke test.
