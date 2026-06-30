# Setup manual — running the system for testing

How to bring up every moving piece on one machine (no hardware, no AWS) and feed
it simulated data. For the **real hardware** bring-up see
[FULL_SYSTEM_TEST.md](FULL_SYSTEM_TEST.md); for the **cloud alert logic** tiers see
[Cloud Server/TESTING.md](Cloud%20Server/TESTING.md); for **go-live** see
[DEPLOYMENT.md](DEPLOYMENT.md).

All commands are **PowerShell** from the repo root
`c:\Users\maazs\Documents\Projects\HVAC_v1.1` unless noted. Each service runs in
its **own terminal** (they're long-running).

---

## 1. The pieces & their ports

| # | Service | Dir | Port | What it is | Needed for testing? |
|---|---------|-----|------|------------|---------------------|
| 1 | **Cloud Server** | `Cloud Server/` | **8002** | FastAPI product backend (auth, readings, alerts, env, crashes, OTA). Multi-tenant. | **Yes — the core.** |
| 2 | **Web dashboard** | `web-dashboard/` | **5173** (dev) | React admin/member UI. In prod it's served *by* the Cloud Server at `:8002/`. | Yes (or use the served build) |
| 3 | **Field console** | `field-console/` | **5174** (dev) | Manufacturer LAN service tool (support token, firmware/OTA). | Optional |
| 4 | **Discovery server** | `Discovery Server/` | **8000** | LAN rendezvous + "site offline" email. Single-site, no accounts. | Optional (legacy path) |
| 5 | **Display node** | `Discovery Server/` | **8001** | On-prem 3D LAN dashboard, ingests readings. | Optional (legacy path) |
| 6 | **Flutter app** | `thread_commissioner/` | — | Phone app: BLE commissioning + cloud monitoring. | When you have a device/emulator |

The **Cloud Server (1)** + **web dashboard (2)** are all you need to exercise the
product. Pieces 4–5 are the older single-site LAN stack and only matter if you're
testing that path.

---

## 2. One-time prerequisites

- **Python** with the **`alpr_dev`** conda env (deps already installed). Activate it
  once per terminal; then run `python`/`uvicorn` directly (don't prefix with
  `conda run` — it buffers server output):
  ```powershell
  conda activate alpr_dev
  ```
  First time only, install deps:
  ```powershell
  pip install -r "Cloud Server\requirements.txt"
  pip install -r "Discovery Server\requirements.txt"
  ```
- **Node.js + npm** (for the two web UIs). First time in each:
  ```powershell
  cd web-dashboard;  npm install; cd ..
  cd field-console;  npm install; cd ..
  ```
- **Flutter SDK** (only for the phone app): `flutter doctor` should be green.
- **Firewall:** to reach these from a phone/another box, allow inbound **8000–8002**
  (and **5173/5174** if serving the dev UIs to other machines).

> **Heads-up on email:** `Cloud Server/.env` is auto-loaded and currently holds live
> Gmail SMTP creds. **Any alert you trip while the server runs will send a real
> email.** To test silently, temporarily set `SMTP_HOST=` (empty) in `.env`, or run
> the server with `$env:SMTP_HOST=""` set in that terminal — alerts then just log
> `[email:skipped] …`.

---

## 3. Quick start — the product (cloud + web), ~2 min

**Terminal A — Cloud Server (`:8002`):**
```powershell
conda activate alpr_dev
cd "c:\Users\maazs\Documents\Projects\HVAC_v1.1\Cloud Server"
$env:BOOTSTRAP_TOKEN = "dev-bootstrap"
python -m uvicorn app:app --host 0.0.0.0 --port 8002
```
Verify: open `http://localhost:8002/health` → `{"ok":true}`. On startup it logs
`[web] serving dashboard from … at /` if a `web-dashboard/dist` build exists, so
`http://localhost:8002/` already shows the dashboard.

**Terminal B — web dashboard with hot-reload (`:5173`), optional:**
```powershell
cd "c:\Users\maazs\Documents\Projects\HVAC_v1.1\web-dashboard"
npm run dev
```
Open `http://localhost:5173`. The dev server proxies `/v1` → `:8002`, so no CORS
setup. (Skip this and just use `http://localhost:8002/` if you don't need live
front-end edits.)

**In the browser:**
1. **Create organization** → org name, bootstrap token `dev-bootstrap`, email,
   password → sign in (you're now admin).
2. **Alerts & Thresholds** → set **High temp** low (e.g. `30`) → Save.
3. Feed a reading (see §6) → it appears under **Live temperatures**; over-limit trips
   an alert.

That's the whole product loop without any hardware.

---

## 4. Field console (manufacturer tool, `:5174`) — optional

Needs a **support token** set on the Cloud Server, else its API is disabled (404).
Stop Terminal A and restart it with the token set:
```powershell
$env:BOOTSTRAP_TOKEN = "dev-bootstrap"
$env:SUPPORT_TOKEN   = "test-support-token-min-24-chars-long"
python -m uvicorn app:app --host 0.0.0.0 --port 8002
```
Then:
```powershell
cd "c:\Users\maazs\Documents\Projects\HVAC_v1.1\field-console"
npm run dev      # http://localhost:5174
```
On its **Connect** screen leave the URL blank (uses the dev proxy → `:8002`) and
enter the support token. You get Fleet Health, Crashes, Env/Readings, Alerts, and
Firmware/OTA publish.

---

## 5. Legacy LAN path — discovery + display node + sim — optional

Only if you're testing the single-site discovery/display stack (the gateway can
post to the display node for its 3D dashboard).

**Terminal C — discovery server (`:8000`):**
```powershell
conda activate alpr_dev
cd "c:\Users\maazs\Documents\Projects\HVAC_v1.1\Discovery Server"
python discovery_server.py
```

**Terminal D — display node (`:8001`):**
```powershell
cd "c:\Users\maazs\Documents\Projects\HVAC_v1.1\Discovery Server"
python display_node.py --discovery http://localhost:8000
# ...or ingest from a real board on serial instead of HTTP:
# python display_node.py --serial COM5 --baud 115200
```
Open `http://localhost:8001/` → the 3D rack grid.

**Terminal E — fake sensors (feeds discovery → display):**
```powershell
cd "c:\Users\maazs\Documents\Projects\HVAC_v1.1\Discovery Server"
python sensor_sim.py --discovery http://localhost:8000 --boxes 12 --probes 8 --period 2
```
Watch the dashboard boxes color by temperature. Stop the sim and within
`HEARTBEAT_TIMEOUT` the discovery server logs/sends the "site offline" alert.

---

## 6. Feeding the Cloud Server test data (no hardware)

A `curl`/HTTP POST is byte-for-byte what the gateway firmware sends, so this is a
faithful end-to-end test.

### 6a. One-shot logic smoke test (fastest)
```powershell
conda activate alpr_dev
cd "c:\Users\maazs\Documents\Projects\HVAC_v1.1\Cloud Server"
python test_local.py        # register → ingest hot reading → alert → clear → ...
```
Expect `ALL CHECKS PASSED`. Runs fully self-contained (its own throwaway DB, email
stubbed out) — doesn't touch your running server.

### 6b. Simulate the gateway against the live server
With the Cloud Server from §3 running, in a new terminal act as the app, then as
the gateway:
```powershell
$base = "http://localhost:8002"

# register a tenant + admin (skip if you already created the org in the browser)
$reg = Invoke-RestMethod -Method Post -Uri "$base/v1/auth/register" -ContentType application/json `
  -Body (@{bootstrap_token="dev-bootstrap"; tenant_name="Acme"; email="a@acme.com"; password="pw123456"} | ConvertTo-Json)
$hdr = @{ Authorization = "Bearer $($reg.token)" }

# mint a gateway API key
$key = (Invoke-RestMethod -Method Post -Uri "$base/v1/apikeys" -Headers $hdr -ContentType application/json `
  -Body (@{label="rig"} | ConvertTo-Json)).api_key

# assign one sensor in the rack layout
$topo = @{ racks = @(@{ id="r1"; name="Rack A"; units = @(@{ id="u1"; name="Unit 1"; ports = @(
  @{ id="p1"; type="intake"; label="Intake 1"; box=1; assignedEui="AABBCCDD00000001" }) }) }) }
Invoke-RestMethod -Method Put -Uri "$base/v1/topology" -Headers $hdr -ContentType application/json `
  -Body (@{topology=$topo} | ConvertTo-Json -Depth 8) | Out-Null

# set a low threshold so a warm reading trips it
Invoke-RestMethod -Method Put -Uri "$base/v1/thresholds" -Headers $hdr -ContentType application/json `
  -Body (@{scope="tenant"; high_c=30; delta_c=10} | ConvertTo-Json) | Out-Null

# POST a hot reading AS THE GATEWAY (same body as Bridge.ino)
Invoke-RestMethod -Method Post -Uri "$base/v1/readings" -Headers @{ "X-API-Key"=$key } -ContentType application/json `
  -Body (@{sensor_id="AABBCCDD00000001"; data="t=45.0,44.0"} | ConvertTo-Json)

# read the alert back
Invoke-RestMethod -Uri "$base/v1/alerts" -Headers $hdr | ConvertTo-Json -Depth 6
```
The server logs `[HVAC ALERT] HIGH TEMPERATURE — Rack A / Unit 1 / Intake 1` (a real
email if SMTP is live), and the dashboard shows the open alert. Post `t=20.0` for the
same sensor to watch it clear (hysteresis). You can also POST `/v1/env` (BME) and
`/v1/crashes` the same way — see [Cloud Server/README.md](Cloud%20Server/README.md).

---

## 7. The Flutter app against the local server — optional

```powershell
cd "c:\Users\maazs\Documents\Projects\HVAC_v1.1\thread_commissioner"
flutter devices
flutter run
```
On the login screen tap the **server-URL** icon → set the base URL:
- **Android emulator:** `http://10.0.2.2:8002` (10.0.2.2 = your PC from the emulator)
- **Real phone (same Wi-Fi):** `http://<your-PC-LAN-IP>:8002` — find it with
  `ipconfig`, and allow port 8002 through the firewall.

Then create/join an org (bootstrap token `dev-bootstrap`) and you're in. BLE
commissioning needs real hardware; the cloud monitoring tabs work against the
simulated data above.

---

## 8. Stopping / resetting

- **Stop a service:** `Ctrl-C` in its terminal.
- **Wipe the cloud DB** (start fresh tenants/readings): stop the server, then delete
  `Cloud Server\cloud.db` (it's recreated on next start in dev/SQLite).
- **Wipe the legacy DBs:** delete `Discovery Server\discovery.db` / `node.db`.
- **Ports already in use:** something's still running — find it with
  `Get-NetTCPConnection -LocalPort 8002` and stop that PID, or pick another `--port`.

---

## 9. Typical "test everything" terminal layout

| Terminal | Command | URL |
|----------|---------|-----|
| A | Cloud Server (`uvicorn … :8002`) | http://localhost:8002 |
| B | `npm run dev` (web-dashboard) | http://localhost:5173 |
| C | `python test_local.py` **or** the §6b gateway-sim snippet | — |
| D *(opt)* | `npm run dev` (field-console) | http://localhost:5174 |
| E *(opt)* | discovery + display + `sensor_sim.py` | http://localhost:8001 |

Start A, open B (or `:8002/` directly), create an org, run C to push readings, and
watch alerts land in the dashboard.
