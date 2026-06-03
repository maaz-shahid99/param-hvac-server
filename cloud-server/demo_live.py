"""
Tier-3 live demo client: drives a RUNNING Cloud Server over real HTTP, acting
first as the app, then as the gateway. Run after starting:
    uvicorn app:app --port 8002
Usage: conda run -n alpr_dev python demo_live.py
"""
import sys
import time
import httpx

BASE = "http://127.0.0.1:8002"
c = httpx.Client(base_url=BASE, timeout=10)


def step(n, msg):
    print(f"\n[{n}] {msg}")


# wait for server
for _ in range(40):
    try:
        if c.get("/health").status_code == 200:
            break
    except Exception:
        time.sleep(0.25)
else:
    print("server not reachable on :8002")
    sys.exit(1)

email = f"demo{int(time.time())}@acme.com"  # unique each run

step(1, "register tenant + admin (as the APP would)")
reg = c.post("/v1/auth/register", json={
    "bootstrap_token": "dev-bootstrap", "tenant_name": "Acme DC",
    "email": email, "password": "pw123456"}).json()
hdr = {"Authorization": f"Bearer {reg['token']}"}
print("    tenant_id:", reg["tenant_id"], "role:", reg["role"])

step(2, "mint a gateway API key")
key = c.post("/v1/apikeys", headers=hdr, json={"label": "rig"}).json()["api_key"]
print("    api_key:", key[:18], "...")

step(3, "push rack layout: Rack A / Unit 1 / Intake 1 -> sensor AABBCCDD00000001")
topo = {"racks": [{"id": "r1", "name": "Rack A", "units": [
    {"id": "u1", "name": "Unit 1", "ports": [
        {"id": "p1", "type": "intake", "label": "Intake 1", "box": 1,
         "assignedEui": "AABBCCDD00000001"}]}]}]}
r = c.put("/v1/topology", headers=hdr, json={"topology": topo}).json()
print("    mapped_sensors:", r["mapped_sensors"])

step(4, "set a LOW threshold (high=30C) so a warm reading trips it")
c.put("/v1/thresholds", headers=hdr, json={"scope": "tenant", "high_c": 30, "delta_c": 10})
print("    threshold saved: high=30C")

step(5, "SIMULATE THE GATEWAY posting a HOT reading (45C) via X-API-Key")
rr = c.post("/v1/readings", headers={"X-API-Key": key},
            json={"sensor_id": "AABBCCDD00000001", "data": "t=45.0,44.0"}).json()
print("    ingested, max_c:", rr["max_c"], "  <-- watch the SERVER log for the alert")

time.sleep(0.3)
step(6, "read open alerts back (as the APP would)")
alerts = c.get("/v1/alerts", headers=hdr).json()["alerts"]
for a in alerts:
    print(f"    ALERT [{a['kind']}] {a['location']}  {a['value']}C (limit {a['threshold']}C)  state={a['state']}")

step(7, "post a COOL reading (20C) -> alert should clear")
c.post("/v1/readings", headers={"X-API-Key": key},
       json={"sensor_id": "AABBCCDD00000001", "data": "t=20.0"})
time.sleep(0.3)
remaining = c.get("/v1/alerts?state=open", headers=hdr).json()["alerts"]
print("    open alerts now:", len(remaining))

print("\nDEMO COMPLETE")
