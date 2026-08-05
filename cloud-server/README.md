# Cloud Server — multi-tenant rack-overheat alerting

The product backend. Ingests readings from gateways, evaluates them against
per-customer thresholds, and alerts the customer (SES email / SNS SMS) when a
server rack or data node overheats — or when a sensor goes silent. It also
serves the **discovery** service at `/discovery` (see `discovery_routes.py`), so
one server on one port covers both planes.

The Flutter app (`thread_commissioner/`) is the control plane (log in, configure
the rack layout, set thresholds, view live temps and alerts); **this** service
is the always-on engine, because a phone is not always running.

## Why this exists / how it differs from the other servers
- **This Cloud Server** — multi-tenant, JWT auth, API-key ingest, the threshold
  engine, and SES/SNS alerts. Every table carries a `tenant_id`.
- `discovery_routes.py` (**mounted here at `/discovery`**) — rendezvous +
  a presence-only "site offline" email. Single-site, no accounts, its own sqlite
  file (`DISCOVERY_DB`). A gateway only needs this server's URL: it derives
  `<cloud_url>/discovery` itself, so there is no second URL to provision.
  These endpoints are **unauthenticated**, matching the old standalone service —
  keep this port LAN-only, or firewall `/discovery/*`, if it is internet-facing.
- `../discovery-server/discovery_server.py` — the same service as a **standalone**
  process on `:8000`. Still supported for sites that want it separate; point the
  gateway's `disc` override at it. Don't run both for one site.
- `../discovery-server/display_node.py` — LAN dashboard + time-series, **no
  threshold logic**, no tenants. **Unchanged** (the gateway still posts to it for
  the local 3D dashboard).

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

## Database migrations (Alembic)
Local dev on SQLite auto-creates the schema, so you don't need Alembic to run
`test_local.py` or develop. **Production owns the schema through Alembic** — the
app no longer `create_all`s on Postgres.

```bash
# point at the target DB, then apply all migrations
export DATABASE_URL=postgresql+psycopg://USER:PASS@HOST:5432/hvac
alembic upgrade head            # run this on every deploy, before starting uvicorn

# after changing a model, generate a migration and review it before committing
alembic revision --autogenerate -m "describe change"
alembic check                   # CI: fails if models drifted from migrations
```

## Deploy on AWS (Phase 1 checklist — needs your AWS account)
1. **RDS Postgres** → set `DATABASE_URL=postgresql+psycopg://USER:PASS@HOST:5432/hvac`, then `alembic upgrade head`.
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
| `GET /v1/current` | JWT | latest temp per **mapped** sensor/probe |
| `GET /v1/alerts?state=` | JWT | open/all alerts |
| `POST /v1/alerts/{id}/ack` | JWT | acknowledge an alert |
| `PUT /v1/recipients` | JWT (admin) | alert email/phone targets |
| `GET/PUT /v1/settings` | JWT (PUT=admin) | `alert_granularity` + `collect_interval_s` |
| `POST /v1/env` | X-API-Key | router/gateway **BME** ingest (temp/hum/pres/voc) |
| `GET /v1/env/current` | JWT | latest BME per device (Environment tab) |
| `GET /v1/env/probes` | JWT | **every** probe of each mapped sensor (labeled, or "Probe N") |
| `GET /v1/env/export.csv` · `GET /v1/readings/export.csv` | JWT | env + per-probe CSV (named) |
| `POST /v1/crashes` | X-API-Key | firmware crash ingest (reset reason, PC, task) |
| `GET /v1/crashes` · `GET /v1/crashes/export.csv` | JWT | crash list + CSV (Diagnostics page) |

## Files
- `app.py` — FastAPI app, endpoints (auth, readings, **env**, **crashes**,
  topology→sensor_map flattening, CSV exports), stale watchdog.
- `db.py` — SQLAlchemy models (all `tenant_id`-scoped): adds `EnvReading`,
  `CrashReport`, and `Tenant.collect_interval_s`. SQLite/Postgres via `DATABASE_URL`.
- `auth.py` — bcrypt passwords, JWT, API-key hashing, request→tenant dependencies.
- `thresholds.py` — high-temp + ΔT evaluation, alert lifecycle (hysteresis/cooldown).
- `notifications.py` — SES/SNS dispatch with log-only fallback.
- `config.py` — env-driven config with safe defaults + a dependency-free `.env` loader.
- `migrations/versions/e7c2a9f0b3d1_*` — adds `env_readings`, `crash_reports`,
  `tenants.collect_interval_s`.
- `scripts/setup_appliance.py` — one-shot on-prem appliance bootstrap (writes `.env`).
- `test_local.py` — end-to-end smoke test (now covers env ingest/current, crashes, CSV).

> **Note on schema drift:** a SQLite DB created before these models existed won't
> have the new columns/tables (`create_all` adds *tables* but never alters
> existing ones). On the appliance, `alembic stamp head` once, then
> `alembic upgrade head` — or recreate `cloud.db` from the current models.
