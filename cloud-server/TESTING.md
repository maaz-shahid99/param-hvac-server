# Local testing runbook — Cloud Server

Tests go from cheapest/most-isolated to closest-to-production. You can stop at
any tier. Everything except Tier 6 runs entirely on your machine (no AWS).

Prereqs already in place: conda env **`alpr_dev`** (deps installed), **PostgreSQL
18** running on `localhost:5432`. Run commands from PowerShell.

---

## Tier 1 — Backend logic on SQLite (fast sanity, ~3 s)
Exercises the whole alert pipeline against a throwaway SQLite file.
```powershell
cd "c:\Users\maazs\Documents\Projects\HVAC_v1.1\Cloud Server"
conda run -n alpr_dev python test_local.py
```
Expect: `ALL CHECKS PASSED` (11 checks). This is the logic test — register, ingest
a hot reading, high-temp + ΔT alerts open, hysteresis clears them, tenant isolation.

---

## Tier 2 — Same suite on REAL Postgres (mirrors AWS RDS)
This is the "storing in AWS" path: identical code, real Postgres dialect. Uses an
isolated throwaway database so nothing else is touched.
```powershell
$env:PGPASSWORD = "YOUR_POSTGRES_PASSWORD"          # the 'postgres' superuser password
psql -U postgres -h localhost -c "DROP DATABASE IF EXISTS hvac_test;"
psql -U postgres -h localhost -c "CREATE DATABASE hvac_test;"

$env:DATABASE_URL = "postgresql+psycopg://postgres:$($env:PGPASSWORD)@localhost:5432/hvac_test"
cd "c:\Users\maazs\Documents\Projects\HVAC_v1.1\Cloud Server"
conda run -n alpr_dev python test_local.py             # prints "Testing against: localhost:5432/hvac_test"
```
Expect: same `ALL CHECKS PASSED`, now on Postgres. Cleanup:
```powershell
psql -U postgres -h localhost -c "DROP DATABASE hvac_test;"
Remove-Item Env:DATABASE_URL
```
> If your password has special characters (`@ : / #`), URL-encode them in DATABASE_URL.

---

## Tier 3 — Run the live server + simulate the gateway (no hardware)
A `curl`/HTTP POST is byte-for-byte what the firmware sends, so this tests the real
ingest → alert path without flashing anything.

**Terminal A — start the server** (point it at Postgres or omit for SQLite):
```powershell
cd "c:\Users\maazs\Documents\Projects\HVAC_v1.1\Cloud Server"
$env:BOOTSTRAP_TOKEN = "dev-bootstrap"
conda run -n alpr_dev uvicorn app:app --host 0.0.0.0 --port 8002
```
**Terminal B — act as the app, then as the gateway:**
```powershell
$base = "http://localhost:8002"

# 1. register a tenant + admin (as the app would)
$reg = Invoke-RestMethod -Method Post -Uri "$base/v1/auth/register" -ContentType application/json `
  -Body (@{bootstrap_token="dev-bootstrap"; tenant_name="Acme"; email="a@acme.com"; password="pw123456"} | ConvertTo-Json)
$hdr = @{ Authorization = "Bearer $($reg.token)" }

# 2. mint a gateway API key
$key = (Invoke-RestMethod -Method Post -Uri "$base/v1/apikeys" -Headers $hdr -ContentType application/json `
  -Body (@{label="rig"} | ConvertTo-Json)).api_key

# 3. push a rack layout with one sensor assigned (as the app's Rack Layout does)
$topo = @{ racks = @(@{ id="r1"; name="Rack A"; units = @(@{ id="u1"; name="Unit 1"; ports = @(
  @{ id="p1"; type="intake"; label="Intake 1"; box=1; assignedEui="AABBCCDD00000001" }) }) }) }
Invoke-RestMethod -Method Put -Uri "$base/v1/topology" -Headers $hdr -ContentType application/json `
  -Body (@{topology=$topo} | ConvertTo-Json -Depth 8)

# 4. set a low threshold so a warm reading trips it
Invoke-RestMethod -Method Put -Uri "$base/v1/thresholds" -Headers $hdr -ContentType application/json `
  -Body (@{scope="tenant"; high_c=30; delta_c=10} | ConvertTo-Json)

# 5. SIMULATE THE GATEWAY posting a hot reading (X-API-Key, same body as Bridge.ino)
Invoke-RestMethod -Method Post -Uri "$base/v1/readings" -Headers @{ "X-API-Key"=$key } -ContentType application/json `
  -Body (@{sensor_id="AABBCCDD00000001"; data="t=45.0,44.0"} | ConvertTo-Json)

# 6. read back the alert
Invoke-RestMethod -Uri "$base/v1/alerts" -Headers $hdr | ConvertTo-Json -Depth 6
```
Expect: **Terminal A** prints `[email:skipped] [HVAC ALERT] HIGH TEMPERATURE — Rack A / Unit 1 / Intake 1`
(that's the alert that would be an email/SMS on AWS), and step 6 shows one open
`high_temp` alert. Post `t=20.0` for the same sensor to watch it clear.

---

## Tier 4 — The Flutter app against the local server
```powershell
cd "c:\Users\maazs\Documents\Projects\HVAC_v1.1\thread_commissioner"
flutter devices
flutter run
```
In the app:
1. Login screen → tap the **server-URL icon** (top-right) → enter the base URL:
   - **Android emulator:** `http://10.0.2.2:8002` (10.0.2.2 = your PC from the emulator)
   - **Real phone (same Wi-Fi):** `http://<your-PC-LAN-IP>:8002` (run `ipconfig`; allow port 8002 through Windows Firewall)
2. Tap **"Set up a new organization"** → org name, bootstrap token `dev-bootstrap`, email, password → **Create & sign in**.
3. Tap the **bell icon** (Alerts & Thresholds) → set High temp `30` → Save.
4. Fire a hot reading from Tier 3 step 5 (or the gateway) → pull-to-refresh →
   the alert appears, live temps show 45°. Create the rack layout in **Rack Layout**
   and confirm it survives an app restart (it's now cloud-synced).

---

## Tier 5 — (optional) Mock SES/SNS so "sending" is exercised
Verifies the boto3 send calls actually fire (not just the log fallback) without AWS:
```powershell
conda run -n alpr_dev python -m pip install moto[ses,sns]
```
Then a small test stands up moto's mock SES/SNS, sets `SES_FROM`/`SNS_SMS_ENABLED`,
triggers an alert, and asserts a message was sent. Ask me to add `test_notify.py`
and I'll wire it.

---

## Tier 6 — Real hardware + AWS (production)
Not local. See `README.md` → "Deploy on AWS": RDS `DATABASE_URL`, SES `SES_FROM`,
SNS, strong `JWT_SECRET`/`BOOTSTRAP_TOKEN`, HTTPS; flash `Bridge.ino`; provision
the gateway with the cloud URL + key; warm a probe → real email/SMS.

---

### What each tier proves
| Tier | Proves | Needs |
|---|---|---|
| 1 | alert logic correct | nothing |
| 2 | **storage works on AWS-grade Postgres** | postgres password |
| 3 | gateway→cloud→alert path end-to-end | nothing (curl = gateway) |
| 4 | the app drives it all | a device/emulator |
| 5 | SES/SNS send calls fire | moto |
| 6 | the real product | hardware + AWS |
