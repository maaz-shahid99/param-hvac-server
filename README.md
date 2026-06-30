# hvac-server

The Python backends for the HVAC monitoring system. Two independent services.

> **Integration contract:** see [PROTOCOL.md](PROTOCOL.md) for the HTTP API, ports,
> auth planes, and wire formats shared with the firmware, app, and web repos.

## Services
| Path | Framework | Port | Role |
|---|---|---|---|
| [cloud-server/](cloud-server/) | FastAPI + SQLAlchemy | **8002** | Multi-tenant cloud: ingest, JWT auth, threshold alerts (email/SMS), OTA, support plane |
| [discovery-server/](discovery-server/) | FastAPI | **8000** (+ display node **8001**) | LAN device discovery + on-prem live dashboard (`display_node.py`) |

The two services do **not** import each other — they deploy independently. Cloud Server
is the backend for `hvac-mobile` and `hvac-web`; the discovery server is the optional
on-prem path.

## Quick start (cloud-server)
```bash
cd cloud-server
python -m pip install -r requirements.txt
cp .env.example .env          # then set JWT_SECRET, BOOTSTRAP_TOKEN, (optional) SUPPORT_TOKEN, SMTP/AWS
uvicorn app:app --port 8002
```
With `ENV=production` the server refuses to start on default/weak secrets — see
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). Never commit a real `.env` (only `.env.example`).

## Quick start (discovery-server)
```bash
cd discovery-server
python -m pip install -r requirements.txt
python discovery_server.py      # :8000   (rendezvous)
python display_node.py          # :8001   (local dashboard) — set DISCOVERY_URL / NODE_IP as needed
```

## Sibling repos
- App: https://github.com/YOUR-ORG/hvac-mobile
- Firmware: https://github.com/YOUR-ORG/hvac-firmware
- Web: https://github.com/YOUR-ORG/hvac-web

Ops docs: [docs/](docs/) (deployment, setup, full-system test). Security: [SECURITY.md](SECURITY.md).
