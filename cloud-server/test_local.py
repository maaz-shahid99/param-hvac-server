"""
Local end-to-end smoke test for the Cloud Server (SQLite, no AWS).

Exercises the full Phase-1 alert loop with FastAPI's TestClient:
  register -> api key -> topology sync -> threshold -> hot reading -> alert
  -> cool reading -> alert clears. Notifications log instead of send.

Run:  conda run -n alpr_dev python test_local.py
"""
import os
import tempfile

# Honour a pre-set DATABASE_URL (e.g. a local Postgres mirroring AWS RDS); else
# default to a throwaway SQLite file. Either way the same suite runs unchanged.
if not os.environ.get("DATABASE_URL"):
    _db = os.path.join(tempfile.gettempdir(), "hvac_cloud_test.db")
    if os.path.exists(_db):
        os.remove(_db)
    os.environ["DATABASE_URL"] = f"sqlite:///{_db}"
os.environ["BOOTSTRAP_TOKEN"] = "test-boot"
os.environ["HYSTERESIS_C"] = "3"
os.environ["JWT_SECRET"] = "test-secret-key-at-least-32-bytes-long-xx"

print(f"Testing against: {os.environ['DATABASE_URL'].split('@')[-1]}")

from fastapi.testclient import TestClient  # noqa: E402
import app as appmod  # noqa: E402
from db import Base, engine  # noqa: E402

# Start from a clean schema so the suite is repeatable on a persistent DB.
Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)

client = TestClient(appmod.app)


def check(cond, msg):
    print(("  PASS" if cond else "  FAIL") + f": {msg}")
    assert cond, msg


with client:  # triggers lifespan (init_db + watchdog)
    print("1) register tenant + admin")
    r = client.post("/v1/auth/register", json={
        "bootstrap_token": "test-boot", "tenant_name": "Acme DC",
        "email": "admin@acme.test", "password": "hunter2",
    })
    check(r.status_code == 200, f"register -> {r.status_code} {r.text}")
    token = r.json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    org_code = r.json()["org_code"]
    tid = r.json()["tenant_id"]
    check(len(org_code) >= 6 and r.json()["role"] == "admin", "register returns org_code + admin role")

    print("2) login round-trip")
    r = client.post("/v1/auth/login", json={"email": "admin@acme.test", "password": "hunter2"})
    check(r.status_code == 200 and "token" in r.json(), "login returns token")
    check(client.post("/v1/auth/login", json={"email": "admin@acme.test", "password": "wrong"}).status_code == 401,
          "bad password rejected")

    print("3) create gateway API key")
    r = client.post("/v1/apikeys", headers=auth, json={"label": "rig"})
    check(r.status_code == 200 and r.json()["api_key"].startswith("hvac_"), "api key issued")
    api_key = r.json()["api_key"]

    print("4) push topology (rack A / unit 1 / intake+exhaust, EUIs assigned)")
    topo = {"racks": [{"id": "r1", "name": "Rack A", "units": [
        {"id": "u1", "name": "Unit 1", "ports": [
            {"id": "p1", "type": "intake", "label": "Intake 1", "box": 1, "assignedEui": "AABBCCDD00000001"},
            {"id": "p2", "type": "exhaust", "label": "Exhaust 1", "box": 2, "assignedEui": "AABBCCDD00000002"},
        ]},
    ]}]}
    r = client.put("/v1/topology", headers=auth, json={"topology": topo})
    check(r.status_code == 200 and r.json()["mapped_sensors"] == 2, "2 sensors mapped from topology")
    r = client.get("/v1/topology", headers=auth)
    check(r.json()["topology"]["racks"][0]["name"] == "Rack A", "topology round-trips")

    print("5) set a low tenant threshold (high=30C, delta=10C)")
    r = client.put("/v1/thresholds", headers=auth,
                   json={"scope": "tenant", "high_c": 30, "delta_c": 10})
    check(r.status_code == 200, "threshold saved")

    print("6) ingest a HOT intake reading (45C) -> high_temp alert should open")
    r = client.post("/v1/readings", headers={"X-API-Key": api_key},
                    json={"sensor_id": "AABBCCDD00000001", "data": "t=45.0,44.2,err"})
    check(r.status_code == 200 and r.json()["max_c"] == 45.0, "hot reading ingested, max_c=45")
    alerts = client.get("/v1/alerts", headers=auth).json()["alerts"]
    high = [a for a in alerts if a["kind"] == "high_temp"]
    check(len(high) == 1 and high[0]["state"] == "open", "one open high_temp alert")
    check(high[0]["location"].startswith("Rack A"), "alert carries Rack/Unit/Port location")

    print("7) ingest a hot exhaust (50C) vs cool intake -> delta alert (50-45=... use cool intake)")
    client.post("/v1/readings", headers={"X-API-Key": api_key},
                json={"sensor_id": "AABBCCDD00000001", "data": "t=20.0"})   # cool intake
    client.post("/v1/readings", headers={"X-API-Key": api_key},
                json={"sensor_id": "AABBCCDD00000002", "data": "t=50.0"})   # hot exhaust -> delta 30 >= 10
    alerts = client.get("/v1/alerts", headers=auth).json()["alerts"]
    check(any(a["kind"] == "delta" and a["state"] == "open" for a in alerts), "delta alert opened (exhaust-intake)")

    print("8) cool BOTH sensors (20C) -> high_temp + delta alerts should clear")
    client.post("/v1/readings", headers={"X-API-Key": api_key},
                json={"sensor_id": "AABBCCDD00000001", "data": "t=20.0,21.0"})
    client.post("/v1/readings", headers={"X-API-Key": api_key},
                json={"sensor_id": "AABBCCDD00000002", "data": "t=22.0,21.5"})
    open_alerts = client.get("/v1/alerts?state=open", headers=auth).json()["alerts"]
    check(not any(a["kind"] == "high_temp" for a in open_alerts), "high_temp alerts cleared by cool readings")
    check(not any(a["kind"] == "delta" for a in open_alerts), "delta alert cleared when ΔT drops")

    print("9) current temps endpoint")
    cur = client.get("/v1/current", headers=auth).json()["sensors"]
    check(len(cur) == 2, "current shows both sensors")

    print("10) tenant isolation: a second tenant sees none of Acme's data")
    r2 = client.post("/v1/auth/register", json={
        "bootstrap_token": "test-boot", "tenant_name": "Other Co",
        "email": "b@other.test", "password": "pw", })
    auth2 = {"Authorization": f"Bearer {r2.json()['token']}"}
    check(client.get("/v1/current", headers=auth2).json()["sensors"] == [], "other tenant sees no sensors")
    check(client.get("/v1/alerts", headers=auth2).json()["alerts"] == [], "other tenant sees no alerts")

    print("11) unauthenticated / bad-key rejection")
    check(client.get("/v1/current").status_code == 401, "no token -> 401")
    check(client.post("/v1/readings", headers={"X-API-Key": "nope"},
                      json={"sensor_id": "x", "data": "t=1"}).status_code == 401, "bad api key -> 401")

    print("12) password reset via email OTP")
    import re
    # Capture the emailed code (no SES locally) by intercepting notify_email.
    sent: list[str] = []
    appmod.notify_email = lambda to, subject, body: sent.append(body)

    # forgot always 200, even for an unknown email (no enumeration), but only a
    # real user gets a code emitted.
    check(client.post("/v1/auth/forgot", json={"email": "nobody@acme.test"}).status_code == 200,
          "forgot unknown email still 200")
    check(sent == [], "no code emitted for unknown email")
    check(client.post("/v1/auth/forgot", json={"email": "admin@acme.test"}).status_code == 200,
          "forgot known email 200")
    code = re.search(r"(\d{6})", sent[-1]).group(1)
    check(len(code) == 6, "6-digit code emitted to the user")

    # wrong code rejected; correct code resets the password.
    check(client.post("/v1/auth/reset", json={
        "email": "admin@acme.test", "otp": "000000", "new_password": "brandnew1",
    }).status_code == 400, "wrong code rejected")
    check(client.post("/v1/auth/reset", json={
        "email": "admin@acme.test", "otp": code, "new_password": "brandnew1",
    }).status_code == 200, "correct code resets password")

    # old password no longer works; new one does; code is single-use.
    check(client.post("/v1/auth/login", json={"email": "admin@acme.test", "password": "hunter2"}).status_code == 401,
          "old password rejected after reset")
    check(client.post("/v1/auth/login", json={"email": "admin@acme.test", "password": "brandnew1"}).status_code == 200,
          "new password works")
    check(client.post("/v1/auth/reset", json={
        "email": "admin@acme.test", "otp": code, "new_password": "again123",
    }).status_code == 400, "code is single-use (cannot replay)")

    print("13) auth rate limiting (per-IP sliding window)")
    import config as cfg
    cfg.AUTH_RATE_MAX = 3                 # tighten + reset the window for a deterministic check
    appmod._rl_hits.clear()
    codes = [client.post("/v1/auth/login",
                         json={"email": "x@y.z", "password": "nope"}).status_code
             for _ in range(5)]
    check(429 in codes, f"login is rate-limited past the cap ({codes})")
    cfg.AUTH_RATE_MAX = 1000              # relax again for the rest of the suite
    appmod._rl_hits.clear()

    print("14) member join request -> admin approval -> notification opt-in")
    from db import SessionLocal as _SL

    def recipients():
        with _SL() as s:
            return appmod.recipients_for(s, tid)

    # A member joins the admin's org by code -> pending, no notifications.
    r = client.post("/v1/auth/join", json={
        "org_code": org_code, "name": "Bob", "email": "bob@acme.test",
        "phone": "+15550001111", "password": "bobpass1"})
    check(r.status_code == 200 and r.json()["status"] == "pending", "join by code -> pending member")
    mauth = {"Authorization": f"Bearer {r.json()['token']}"}
    check(client.get("/v1/me", headers=mauth).json()["status"] == "pending", "member /me shows pending")
    check(client.post("/v1/auth/join", json={"org_code": "BADCODE9", "email": "x@x.x",
          "password": "pw"}).status_code == 404, "unknown org code rejected")

    pend = client.get("/v1/members?state=pending", headers=auth).json()["members"]
    bob = next(m for m in pend if m["email"] == "bob@acme.test")
    check(bob["status"] == "pending", "admin sees the pending join request")
    em, ph = recipients()
    check("bob@acme.test" not in em and "+15550001111" not in ph, "pending member gets NO notifications")

    # A non-admin can VIEW the roster but cannot manage it.
    check(client.get("/v1/members", headers=mauth).status_code == 200, "member can view the roster (read-only)")
    check(client.post(f"/v1/members/{bob['id']}/approve", headers=mauth).status_code == 403,
          "member can't approve (admin only)")

    # Admin approves, then opts Bob into email, then SMS.
    check(client.post(f"/v1/members/{bob['id']}/approve", headers=auth).status_code == 200, "admin approves member")
    em, ph = recipients()
    check("bob@acme.test" not in em, "approved-but-not-opted-in member still gets no email")
    client.put(f"/v1/members/{bob['id']}/notifications", headers=auth, json={"email_enabled": True})
    em, ph = recipients()
    check("bob@acme.test" in em, "admin enabling email adds member to recipients")
    client.put(f"/v1/members/{bob['id']}/notifications", headers=auth, json={"sms_enabled": True})
    em, ph = recipients()
    check("+15550001111" in ph, "admin enabling SMS adds member's phone to recipients")
    client.put(f"/v1/members/{bob['id']}/notifications", headers=auth, json={"email_enabled": False, "sms_enabled": False})
    em, ph = recipients()
    check("bob@acme.test" not in em and "+15550001111" not in ph, "admin can revoke email + SMS")

    # Rejected member can't log in.
    check(client.post(f"/v1/members/{bob['id']}/reject", headers=auth).status_code == 200, "admin rejects member")
    check(client.post("/v1/auth/login", json={"email": "bob@acme.test", "password": "bobpass1"}).status_code == 403,
          "rejected member is denied login")

    print("15) commissioned-device roster: additive merge + delete + isolation")
    # Phone A registers two devices.
    client.put("/v1/devices", headers=auth, json={"devices": [
        {"eui": "AABBCCDD00000001", "kind": "sensor", "role": ""},
        {"eui": "58E6C5FFFE111BB0", "kind": "gateway", "role": "G"}]})
    devs = client.get("/v1/devices", headers=auth).json()["devices"]
    check(len(devs) == 2, "two devices registered")
    check({d["eui"] for d in devs} == {"aabbccdd00000001", "58e6c5fffe111bb0"}, "euis lower-cased")
    # Phone B (partial view) PUTs only ONE -> additive: must NOT wipe the other.
    client.put("/v1/devices", headers=auth, json={"devices": [
        {"eui": "AABBCCDD00000001", "kind": "sensor", "role": ""}]})
    devs = client.get("/v1/devices", headers=auth).json()["devices"]
    check(len(devs) == 2, "partial PUT does not remove the unlisted device (additive merge)")
    # Explicit delete removes one.
    check(client.delete("/v1/devices/58e6c5fffe111bb0", headers=auth).status_code == 200, "delete device")
    devs = client.get("/v1/devices", headers=auth).json()["devices"]
    check(len(devs) == 1 and devs[0]["eui"] == "aabbccdd00000001", "device removed by DELETE")
    # Tenant isolation: the other org sees none of Acme's roster.
    check(client.get("/v1/devices", headers=auth2).json()["devices"] == [], "other tenant sees no devices")
    # Naming: a PUT with a name round-trips; a later empty-name PUT must NOT clear it.
    client.put("/v1/devices", headers=auth, json={"devices": [
        {"eui": "AABBCCDD00000001", "kind": "sensor", "role": "", "name": "Top exhaust"}]})
    check(client.get("/v1/devices", headers=auth).json()["devices"][0].get("name") == "Top exhaust",
          "device name round-trips")
    client.put("/v1/devices", headers=auth, json={"devices": [
        {"eui": "AABBCCDD00000001", "kind": "sensor", "role": "", "name": ""}]})
    check(client.get("/v1/devices", headers=auth).json()["devices"][0].get("name") == "Top exhaust",
          "empty-name PUT does not clear a set name")

    print("16) per-probe mapping: one sensor's two probes -> two exhausts, probe-mode alerting")
    r = client.post("/v1/auth/register", json={
        "bootstrap_token": "test-boot", "tenant_name": "Probe Co",
        "email": "admin@probe.test", "password": "hunter2"})
    pauth = {"Authorization": f"Bearer {r.json()['token']}"}
    pkey = client.post("/v1/apikeys", headers=pauth, json={"label": "rig"}).json()["api_key"]
    # One C6 (EUI) whose two DS18B20 probes (by ROM) feed two different exhausts.
    eui = "ee0000000000aaaa"
    ptopo = {"racks": [{"id": "r1", "name": "Rack Z", "units": [
        {"id": "u1", "name": "Unit 1", "ports": [
            {"id": "p1", "type": "exhaust", "label": "Exhaust 1", "box": 1,
             "assignedEui": eui, "assignedProbeRom": "28ff01"},
        ]},
        {"id": "u2", "name": "Unit 2", "ports": [
            {"id": "p2", "type": "exhaust", "label": "Exhaust 2", "box": 2,
             "assignedEui": eui, "assignedProbeRom": "28ff02"},
        ]},
    ]}]}
    r = client.put("/v1/topology", headers=pauth, json={"topology": ptopo})
    check(r.json()["mapped_sensors"] == 2, "one sensor's 2 probes mapped to 2 ports")
    client.put("/v1/thresholds", headers=pauth, json={"scope": "tenant", "high_c": 40, "delta_c": 99})
    check(client.put("/v1/settings", headers=pauth, json={"alert_granularity": "probe"}).status_code == 200,
          "alert granularity set to probe")
    # probe 28ff01 is hot (55), 28ff02 is cool (20) -> exactly ONE high_temp alert.
    client.post("/v1/readings", headers={"X-API-Key": pkey},
                json={"sensor_id": eui.upper(), "data": "t=28ff01:55.0,28ff02:20.0"})
    palerts = client.get("/v1/alerts?state=open", headers=pauth).json()["alerts"]
    phigh = [a for a in palerts if a["kind"] == "high_temp"]
    check(len(phigh) == 1, "exactly one probe alerts (only the hot exhaust)")
    check(phigh[0]["location"] == "Rack Z / Unit 1 / Exhaust 1", "alert names the hot probe's own exhaust")
    cur = client.get("/v1/current", headers=pauth).json()["sensors"]
    temps = sorted(s["temp"] for s in cur)
    check(len(cur) == 2 and temps == [20.0, 55.0], "current shows each probe's own temp at its exhaust")
    # Cooling the hot probe clears its alert independently.
    client.post("/v1/readings", headers={"X-API-Key": pkey},
                json={"sensor_id": eui, "data": "t=28ff01:25.0,28ff02:20.0"})
    open_high = [a for a in client.get("/v1/alerts?state=open", headers=pauth).json()["alerts"]
                 if a["kind"] == "high_temp"]
    check(not open_high, "probe alert clears when that probe cools")

print("\nALL CHECKS PASSED")
