# Full‑system test — sensor → mesh → gateway → backend → app

End‑to‑end bring‑up of the whole chain on real hardware. Each stage lists the
**serial / log marker** that proves it worked, so if the chain breaks you know
exactly which hop failed. Work top‑to‑bottom; don't move on until the marker
for the current stage appears.

```
SED sensor (C6)  --UDP ff03::2:1234-->  Commissioner C6  --UART-->  Bridge C3
   t=csv every 10s                       [UDP_RX]                    Wi-Fi uplink
                                                                       |  |
                                              display node /ingest  <--+  +-->  Cloud /v1/readings
                                              (LAN 3D dashboard)              (threshold engine -> alert)
   Phone app <--BLE--> Bridge C3 (commission, provision, NODES?, MAP)
   Phone app <--HTTPS--> Cloud Server (login, topology, thresholds, alerts)
```

## What you need
- ≥1 **gateway unit** = paired **Commissioner (C6)** + **Bridge (C3)** (UART TX21/RX20).
- ≥1 **sensor node** flashed with `SED_SENSOR_BARE` + DS18B20 probe(s) on D4.
- A **PC on the same LAN** running the backend, and a **phone** with the app.
- The phone and gateway on the **same Wi‑Fi**; note the PC's LAN IP (`ipconfig`).

---

## Stage 0 — Backend up (PC)
Two terminals — the Cloud Server now serves discovery itself at `/discovery`, so
there's no separate discovery process to start. Cloud Server can be local now,
AWS later.
```powershell
cd "<repo>\hvac-server\cloud-server"
$env:BOOTSTRAP_TOKEN="dev-bootstrap"
conda run -n alpr_dev python -m uvicorn app:app --host 0.0.0.0 --port 8002

cd "<repo>\hvac-server\discovery-server"
conda run -n alpr_dev python display_node.py                # :8001  (LAN 3D dashboard)
```
**Marker:** `http://<PC-IP>:8001/` shows the 3D grid; `http://<PC-IP>:8002/health`
returns `{"ok":true}`; `http://<PC-IP>:8002/discovery/status` returns the presence
JSON (proves the merged discovery plane is live).

> Firewall: allow inbound 8001/8002 so the gateway and phone can reach the PC.
> (Only add 8000 if you deliberately run `discovery_server.py` standalone.)

## Stage 1 — Flash firmware
- **Commissioner (C6)** — ESP‑IDF: `idf.py set-target esp32c6 && idf.py build flash monitor`.
- **Bridge (C3)** — Arduino IDE, board **XIAO ESP32‑C3**, upload `Bridge.ino`.
  (No URL to edit: discovery is derived from the `cloud` URL you provision in
  Stage 3. `DEFAULT_DISCOVERY_URL` is intentionally empty.)
- **Sensor** — Arduino IDE, board **ESP32‑C6/H2**, upload `SED_SENSOR_BARE.ino`.
  **Marker:** sensor serial prints `[HW] EUI-64: …` — **write this EUI down**, it's the
  device identity and what its QR encodes.

## Stage 2 — Gateway online + app session
1. Power the gateway unit. **C6 marker:** boot role decision, then `GW_ROLE LEADER`
   once it forms/attaches. **C3 marker:** `[BOOT] Discovery server: …` and
   `[BOOT] Cloud alerting: … (key …)`.
2. App: **log in** (Stage uses the Cloud Server) → set server URL `http://<PC-IP>:8002`,
   register an org (bootstrap token `dev-bootstrap`).
3. App: **scan/connect** the Bridge over BLE → **AUTH** with the PIN.
   **C3 marker:** auth handshake OK; app shows authenticated.

## Stage 3 — Provision Wi‑Fi + cloud (one step)
App → **Router Setup** dialog → enter SSID/pass, optionally Thread net name,
`cloud = http://<PC-IP>:8002`, then tap **＋** on the Gateway API Key field to
**mint a key** → **Provision**. **Leave `disc` blank** — the gateway derives
`http://<PC-IP>:8002/discovery` from the cloud URL.
- **C3 markers:** `STATUS CONNECTING_WIFI` → Wi‑Fi connected; on next boot/log
  `[BOOT] Cloud alerting: http://<PC-IP>:8002 (key set)` and
  `[BOOT] Discovery server: http://<PC-IP>:8002/discovery` (the derived value).
- **C6 marker:** forms the mesh (`FORM_NET`) and becomes **Leader** → C3 brings up
  Wi‑Fi + BLE (leadership‑gated).
- **Backend marker:** `display_node` logs the gateway presence; discovery `/discover`
  lists the forwarder.

## Stage 4 — Commission the sensor
1. App: **Scan QR** of the sensor (from `QR codes/`, encodes its EUI + PSKd `J01NME`).
2. App sends signed `add <eui> <pskd>` → relayed to the C6 commissioner.
   **C6 markers:** commissioner `ACTIVE`, joiner admitted (watch for the self‑heal
   re‑petition path if you see `ADD_FAILED 13`, it retries automatically).
   **Sensor markers:** `[JOINER RADAR] …` scanning → keys saved → reboot →
   `OT_DEVICE_ROLE_CHILD`.

## Stage 5 — Verify the data path, hop by hop
Once the sensor is a CHILD it reports every 10 s. Confirm each hop's marker:
1. **Sensor:** `[UDP] Packet sent: EUI=<hex>;t=23.1,24.0,…`
2. **Commissioner C6:** `[UDP_RX] … EUI=<hex>;t=<csv>`
3. **Bridge C3:** `[FWD] <eui> -> http://<PC-IP>:8001/ingest (200)` **and**
   `[CLOUD] <eui> -> http://<PC-IP>:8002/v1/readings (200)`
4. **display_node:** the reading appears; the 3D dashboard box updates.
5. **Cloud Server:** `POST /v1/readings 200`; in the app's **Alerts & Thresholds**
   screen the sensor shows under **Live temperatures**.

## Stage 6 — Map the sensor to a rack location
App → **Rack Layout** → add Rack / Unit / Intake+Exhaust ports → on a port tap
**Assign** → the device dropdown lists the live EUI (tap Refresh → `NODES?`
round‑trips: `NODES_BEGIN/NODE|…/NODES_END`). Assign it.
- **C3 marker:** `[MAP] <eui> -> box<N>-A (Rack / Unit / Port)` POSTed to display node.
- **Cloud marker:** topology PUT returns `mapped_sensors: N`; the live temp now
  shows the **Rack / Unit / Port** label instead of the raw EUI.

## Stage 7 — The actual product: an overheat alert
1. App → **Alerts & Thresholds** → set **High temp** low (e.g. 30 °C) → Save.
2. **Warm the probe** (hand/hair‑dryer) so a reading exceeds 30 °C.
3. **Within ~10 s:**
   - **Cloud Server log:** `[HVAC ALERT] HIGH TEMPERATURE — Rack / Unit / Port`.
     On AWS this is a real **SES email + SNS SMS**; locally it logs (`[email:skipped]`).
   - **App:** the open alert appears in **Alerts & Thresholds** (pull to refresh);
     ACK it to acknowledge.
4. Let the probe cool below ~27 °C → the alert **clears** (hysteresis).
5. **Stale test:** unplug/reset the sensor; after `STALE_AFTER_S` (180 s default)
   a **SENSOR OFFLINE** alert opens; it clears when the sensor reports again.

## Stage 8 — (optional) resilience
- **Failover:** power off the gateway unit; a standby unit's C6 re‑elects Leader,
  its C3 takes the uplink (`GW_ROLE LEADER`), readings resume. (30 s stand‑down grace.)
- **Cross‑device sync:** sign in on a second phone → the rack layout you built in
  Stage 6 is already there (cloud topology sync).
- **OTA:** App → System → Update This Gateway / Update Whole Fleet.

---

## Troubleshooting — find the broken hop by its missing marker
| Symptom | Likely hop | Check |
|---|---|---|
| No `[HW] EUI-64` on sensor | sensor flash | re‑flash `SED_SENSOR_BARE`, board = C6/H2 |
| Sensor stuck `[JOINER RADAR]` | commissioning | commissioner must be **ACTIVE**; PSKd `J01NME` matches; channel 15 |
| `[UDP] sent` but no `[UDP_RX]` | mesh/UDP | sensor is CHILD? same network? group `ff03::2`:1234 |
| `[UDP_RX]` but no `[FWD]` | C3 link / node URL | C3 discovered the node? `discoverNode()` / `disc` URL right? |
| `[FWD] 200` but no `[CLOUD]` | cloud provisioning | `[BOOT] Cloud alerting … (key set)`? re‑provision `cloud`/`cloudKey` |
| `[CLOUD]` 401 | API key/tenant | mint a fresh key in the app, re‑provision |
| Reading shows but no alert | threshold | threshold set low enough? value above limit + cooldown not active? |
| App can't reach cloud | network/URL | phone↔PC firewall; emulator uses `10.0.2.2`, real phone uses PC LAN IP |
| App can't see Bridge | BLE/leadership | only the **Leader** advertises; is this unit Leader (`GW_ROLE LEADER`)? |

## Key constants (must match across the chain)
- Join PSKd: **`J01NME`** (sensor `pskd` == Commissioner `ROUTER_JOIN_PSKD`).
- Sensor data: UDP **`ff03::2` : 1234**. Config‑sync: **`ff03::1` : 1235** (HMAC‑signed).
- UART C3↔C6: **TX=21 / RX=20 @ 115200**.
- Cloud ingest: `POST /v1/readings` with header **`X-API-Key`**.
