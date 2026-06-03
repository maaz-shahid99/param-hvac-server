# HVAC Discovery + Rack Monitor

Three cooperating pieces:

```
  ┌──────────────┐  heartbeat (presence = internet up)    ┌────────────────────┐
  │   sensors    │ ─────────────────────────────────────► │  discovery_server  │
  │ box{n}-A/B   │ ◄──────── /discover (node LAN ip) ──── │   (cloud / remote) │
  └──────┬───────┘                                        │  sqlite + email    │
         │  P2P /ingest (probe temps)                     └─────────┬──────────┘
         ▼                                                          │ if no heartbeat
  ┌──────────────┐   serves dashboard + long-poll                   ▼ → send email
  │ display_node │   (on the user's computer, LAN)            ops@... gets alerted
  │  + browser   │
  └──────────────┘
```

* **`discovery_server.py`** — runs somewhere with reliable internet (a cloud
  box). Sensors heartbeat it; while any sensor reports, the site is "online".
  If every sensor goes silent for `HEARTBEAT_TIMEOUT` it assumes the site lost
  internet and **emails an alert** (once per outage). It hands out the
  forwarding node's **LAN ip** via `/discover` so sensors talk to the node
  **peer-to-peer** — bulk data never round-trips the cloud. Keeps a little
  state in **sqlite3** and forwards "fetch data" events to sensors over a
  long-poll. Mobile devices receive routed readings via `/mobile/poll`.

* **`display_node.py`** — runs on the user's computer next to the rack. Ingests
  readings over **serial** (ESP/STM32 plugged in) *or* **HTTP** (`/ingest`),
  announces its LAN ip to the discovery server, stores readings, and serves the
  dashboard with a **long-poll** so the CPU stays idle until data arrives.

* **`static/`** — the dashboard: a **3D wire-mesh** of 12 rack boxes (each with
  2 sensor markers), live-colored by temperature; **click a box** for analytics
  (trend + per-probe charts); and an **Excel-style log** with CSV export.

## Quick start (all on one machine for testing)

```bash
cd Paramvidya/discovery
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 1. discovery server (cloud role)
python discovery_server.py                      # :8000

# 2. display node (user's computer)  — new terminal
python display_node.py --discovery http://localhost:8000   # :8001
#   ...or with a real ESP on serial:
#   python display_node.py --serial /dev/ttyACM0 --baud 115200

# 3. fake sensors  — new terminal
python sensor_sim.py --discovery http://localhost:8000

# open the dashboard
xdg-open http://localhost:8001/
```

Stop the sensor sim and within `HEARTBEAT_TIMEOUT` seconds the discovery server
logs/sends the offline email (set SMTP_* in `.env` to actually send; otherwise
it prints the alert).

## Wiring real sensors

* **Serial / ESP path** — have the ESP print one line per reading, either JSON
  `{"sensor_id":"box3-A","probes":[23.1, ...]}` or CSV `box3-A,23.1,23.4,...`.
* **WiFi / HTTP path** — POST the same JSON to `http://<node-ip>:8001/ingest`.
  Sensors discover `<node-ip>` from the discovery server's `/discover`.

`sensor_id` of the form `box<N>-<A|B>` auto-maps to box/slot; or send explicit
`"box"` and `"slot"` fields. Probe count is read per-message, so reconfiguring a
sensor from 8 probes to any other count just works.

## Key endpoints

Discovery server (`:8000`)
| method | path | who | purpose |
|---|---|---|---|
| POST | `/register/sensor` | sensors | heartbeat / presence |
| POST | `/register/forwarder` | node | announce LAN ip |
| GET  | `/discover` | sensors, app | get node LAN ip for P2P |
| POST | `/data` | sensors | store + route reading |
| POST | `/fetch` | ops | queue a fetch-data event |
| GET  | `/poll?sensor_id=` | sensors | long-poll for fetch events |
| GET  | `/mobile/poll` | mobile | long-poll routed readings |
| GET  | `/status` | anyone | health snapshot |

Display node (`:8001`)
| method | path | purpose |
|---|---|---|
| POST | `/ingest` | http ingest of a reading |
| GET  | `/poll` | browser long-poll for live readings |
| GET  | `/history?box=&limit=` | recent readings |
| GET  | `/config` | box/probe layout |
| GET  | `/` | the dashboard |
```
