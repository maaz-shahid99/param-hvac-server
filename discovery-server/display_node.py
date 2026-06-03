"""
Display / Data-Forwarding Node
==============================

Runs on the user's computer (the machine physically near the rack). It is the
node whose LAN ip the discovery server hands out. It does three things:

  1. Ingests sensor data, by EITHER:
       - serial  : an ESP (or STM32) plugged in over USB streaming lines, OR
       - http     : sensors / the discovery server POST to /ingest
     Both paths funnel into the same place, so the UI doesn't care which.

  2. Announces itself (node_id + LAN ip + port) to the discovery server every
     few seconds so the discovery server can advertise our local ip for P2P.

  3. Serves the live dashboard (static/) and a long-poll /poll endpoint that
     the browser parks on — the UI updates the instant data shows up and the
     CPU stays idle in between.

Data model
----------
  12 rack boxes, each fitted with 2 sensors (slot A and slot B). Each sensor
  reports a list of probe temperatures (default 8 probes, reconfigurable). A
  reading is mapped sensor -> box -> slot and pushed live to the 3D mesh.

Run:
    pip install -r requirements.txt
    python display_node.py                 # http ingest only
    python display_node.py --serial /dev/ttyACM0 --baud 115200
    # then open http://localhost:8001/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

# --------------------------------------------------------------------------- #
# Config (overridable by CLI / env)                                            #
# --------------------------------------------------------------------------- #

NODE_ID = os.environ.get("NODE_ID", socket.gethostname())
LISTEN_PORT = int(os.environ.get("NODE_PORT", "8001"))
DISCOVERY_URL = os.environ.get("DISCOVERY_URL", "http://localhost:8000")
ANNOUNCE_INTERVAL = float(os.environ.get("ANNOUNCE_INTERVAL", "5"))
LONG_POLL_TIMEOUT = float(os.environ.get("LONG_POLL_TIMEOUT", "25"))
DB_PATH = os.environ.get("NODE_DB", os.path.join(HERE, "node.db"))

NUM_BOXES = int(os.environ.get("NUM_BOXES", "12"))
DEFAULT_PROBES = int(os.environ.get("DEFAULT_PROBES", "8"))

# Force the LAN ip announced to the discovery server (overrides auto-detect).
# Use this when the PC has multiple NICs (VPN/Docker/hotspot) and auto-detect
# picks an interface the gateway can't reach (e.g. 172.16.x.x).
NODE_IP = os.environ.get("NODE_IP", "")

# Set by CLI for the serial reader thread.
SERIAL_PORT: str | None = None
SERIAL_BAUD = 115200


# --------------------------------------------------------------------------- #
# Local store                                                                  #
# --------------------------------------------------------------------------- #

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS readings (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        REAL,
                box       INTEGER,
                slot      TEXT,
                sensor_id TEXT,
                probes    TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rd_ts ON readings(ts)")
        # EUI-64 -> physical box/slot + human-readable location label, assigned
        # at commissioning. box/slot drive the legacy 3D grid; label is the
        # rich "Rack / Unit / Port" location chosen in the app.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sensor_map (
                eui   TEXT PRIMARY KEY,
                box   INTEGER,
                slot  TEXT,
                label TEXT
            )
            """
        )
        # Backfill the label column on databases created before it existed.
        try:
            conn.execute("ALTER TABLE sensor_map ADD COLUMN label TEXT")
        except sqlite3.OperationalError:
            pass  # column already present


# EUI -> (box, slot, label) cache, loaded from sensor_map. Keys are lower-case hex.
EUI_MAP: dict[str, tuple[int, str, str]] = {}


def load_map() -> None:
    EUI_MAP.clear()
    with db() as conn:
        for r in conn.execute("SELECT eui, box, slot, label FROM sensor_map"):
            EUI_MAP[r["eui"].lower()] = (
                int(r["box"]), (r["slot"] or "A").upper(), r["label"] or "")
    print(f"[map] loaded {len(EUI_MAP)} EUI->location mappings")


# --------------------------------------------------------------------------- #
# Long-poll broker for the browser                                            #
# --------------------------------------------------------------------------- #

class Broker:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, message: Any) -> None:
        """Thread-safe: the serial reader thread calls this too."""
        for q in list(self._subs):
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(q.put_nowait, message)
            else:
                q.put_nowait(message)


broker = Broker()


# --------------------------------------------------------------------------- #
# Sensor -> box mapping                                                         #
# --------------------------------------------------------------------------- #
# A reading can name its box/slot explicitly, or use a sensor_id that we map.
# By convention sensor ids look like "box3-A" / "box3-B"; anything else gets
# hashed onto a box so it still shows up somewhere instead of being dropped.

def map_to_box(sensor_id: str, box: int | None, slot: str | None) -> tuple[int, str, str]:
    if box is not None:
        # An explicit box still picks up its commissioned label if we have one.
        label = ""
        mapped = EUI_MAP.get(sensor_id.lower())
        if mapped:
            label = mapped[2]
        return int(box), (slot or "A").upper(), label

    # Commissioned EUI -> location table takes priority (sensors report their EUI).
    mapped = EUI_MAP.get(sensor_id.lower())
    if mapped:
        return mapped

    sid = sensor_id.lower()
    if sid.startswith("box"):
        try:
            rest = sid[3:]
            num_part, _, slot_part = rest.partition("-")
            b = int(num_part)
            s = (slot_part or "A").upper()
            if 1 <= b <= NUM_BOXES and s in ("A", "B"):
                return b, s, ""
        except ValueError:
            pass

    # Fallback: deterministic placement so unknown sensors are still visible.
    h = sum(ord(c) for c in sensor_id)
    return (h % NUM_BOXES) + 1, "AB"[h % 2], ""


def ingest_reading(sensor_id: str, probes: list[float],
                   box: int | None = None, slot: str | None = None,
                   ts: float | None = None) -> dict[str, Any]:
    """Single entry point for BOTH serial and http ingest paths."""
    ts = ts or time.time()
    b, s, label = map_to_box(sensor_id, box, slot)
    record = {
        "ts": ts, "box": b, "slot": s, "label": label,
        "sensor_id": sensor_id, "probes": probes,
    }
    with db() as conn:
        conn.execute(
            "INSERT INTO readings(ts, box, slot, sensor_id, probes) "
            "VALUES(?,?,?,?,?)",
            (ts, b, s, sensor_id, json.dumps(probes)),
        )
    broker.publish(record)
    return record


# --------------------------------------------------------------------------- #
# Serial reader (optional, runs in a daemon thread)                            #
# --------------------------------------------------------------------------- #
# Expected line formats (either works):
#   JSON : {"sensor_id":"box3-A","probes":[23.1,...]}
#   CSV  : box3-A,23.1,23.4,24.0,...          (sensor_id then probe temps)

def _parse_serial_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    if line.startswith("{"):
        try:
            obj = json.loads(line)
            if "sensor_id" in obj and "probes" in obj:
                return obj
        except json.JSONDecodeError:
            return None
        return None
    # CSV fallback
    parts = [p.strip() for p in line.split(",") if p.strip()]
    if len(parts) < 2:
        return None
    sensor_id = parts[0]
    try:
        probes = [float(p) for p in parts[1:]]
    except ValueError:
        return None
    return {"sensor_id": sensor_id, "probes": probes}


def serial_reader_thread(port: str, baud: int) -> None:
    try:
        import serial  # pyserial
    except ImportError:
        print("[serial] pyserial not installed; serial ingest disabled")
        return

    while True:
        try:
            print(f"[serial] opening {port} @ {baud}")
            with serial.Serial(port, baud, timeout=1) as ser:
                print(f"[serial] connected to {port}")
                while True:
                    raw = ser.readline()
                    if not raw:
                        continue
                    obj = _parse_serial_line(raw.decode("utf-8", "ignore"))
                    if obj:
                        ingest_reading(
                            obj["sensor_id"], obj["probes"],
                            obj.get("box"), obj.get("slot"),
                        )
        except Exception as exc:                       # noqa: BLE001
            print(f"[serial] error: {exc}; retrying in 3s")
            time.sleep(3)


# --------------------------------------------------------------------------- #
# Announce to discovery server                                                 #
# --------------------------------------------------------------------------- #

def lan_ip() -> str:
    """Best guess at this machine's LAN ip (the one a sensor would reach)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))        # no packet actually sent
        return s.getsockname()[0]
    except Exception:                      # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


async def announce_loop() -> None:
    ip = NODE_IP or lan_ip()
    print(f"[announce] advertising LAN ip {ip}:{LISTEN_PORT} to discovery server")
    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            try:
                await client.post(
                    f"{DISCOVERY_URL}/register/forwarder",
                    json={"node_id": NODE_ID, "local_ip": ip, "port": LISTEN_PORT},
                )
            except Exception as exc:                   # noqa: BLE001
                print(f"[announce] could not reach discovery server: {exc}")
            await asyncio.sleep(ANNOUNCE_INTERVAL)


# --------------------------------------------------------------------------- #
# FastAPI app                                                                  #
# --------------------------------------------------------------------------- #

# Parse "t=23.1,24.0,err,..." (or a bare CSV) into a probe list; "err" -> None.
def parse_probe_csv(data: str) -> list[float | None]:
    if "=" in data:
        data = data.split("=", 1)[1]
    out: list[float | None] = []
    for tok in data.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if tok.lower() == "err":
            out.append(None)
        else:
            try:
                out.append(float(tok))
            except ValueError:
                out.append(None)
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    load_map()
    broker.bind_loop(asyncio.get_running_loop())
    tasks = [asyncio.create_task(announce_loop())]
    if SERIAL_PORT:
        threading.Thread(
            target=serial_reader_thread,
            args=(SERIAL_PORT, SERIAL_BAUD),
            daemon=True,
        ).start()
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()


app = FastAPI(title="HVAC Display Node", lifespan=lifespan)


class IngestBody(BaseModel):
    sensor_id: str
    probes: list[float | None] = Field(default_factory=list)
    data: str | None = None              # raw "t=23.1,24.0,err" form (gateway path)
    box: int | None = None
    slot: str | None = None
    ts: float | None = None


@app.post("/ingest")
def ingest(body: IngestBody):
    """HTTP ingest path — sensors / gateway / discovery server POST readings here.
    Accepts either a `probes` list or a raw `data` CSV string."""
    probes = body.probes
    if not probes and body.data:
        probes = parse_probe_csv(body.data)
    rec = ingest_reading(body.sensor_id, probes, body.box, body.slot, body.ts)
    return {"ok": True, "record": rec}


class MapBody(BaseModel):
    eui: str
    box: int
    slot: str = "A"
    label: str = ""


@app.post("/map")
def set_map(body: MapBody):
    """Assign a sensor's EUI-64 to a physical location (called at commissioning).

    box/slot keep the legacy 3D grid working; label is the rich
    "Rack / Unit / Port" location chosen in the app."""
    eui = body.eui.lower()
    slot = (body.slot or "A").upper()
    label = body.label or ""
    with db() as conn:
        conn.execute(
            "INSERT INTO sensor_map(eui, box, slot, label) VALUES(?,?,?,?) "
            "ON CONFLICT(eui) DO UPDATE SET box=?, slot=?, label=?",
            (eui, body.box, slot, label, body.box, slot, label),
        )
    EUI_MAP[eui] = (body.box, slot, label)
    print(f"[map] {eui} -> box{body.box}-{slot} ({label})")
    return {"ok": True, "eui": eui, "box": body.box, "slot": slot, "label": label}


@app.get("/map")
def get_map():
    return {"map": [{"eui": k, "box": v[0], "slot": v[1], "label": v[2]}
                    for k, v in EUI_MAP.items()]}


@app.get("/poll")
async def poll():
    """Browser long-poll: returns the next reading as soon as it lands."""
    q = broker.subscribe()
    try:
        msg = await asyncio.wait_for(q.get(), timeout=LONG_POLL_TIMEOUT)
        return {"reading": msg}
    except asyncio.TimeoutError:
        return {"reading": None}
    finally:
        broker.unsubscribe(q)


@app.get("/config")
def config():
    return {"num_boxes": NUM_BOXES, "default_probes": DEFAULT_PROBES,
            "node_id": NODE_ID}


@app.get("/history")
def history(box: int | None = None, limit: int = 500):
    q = "SELECT ts, box, slot, sensor_id, probes FROM readings"
    params: list[Any] = []
    if box is not None:
        q += " WHERE box = ?"
        params.append(box)
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    with db() as conn:
        rows = conn.execute(q, params).fetchall()
    return {"readings": [
        {"ts": r["ts"], "box": r["box"], "slot": r["slot"],
         "sensor_id": r["sensor_id"], "probes": json.loads(r["probes"])}
        for r in rows
    ]}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# --- OTA firmware hosting ---------------------------------------------------
# Drop the built images + a manifest into ./firmware:
#   firmware/manifest.json  e.g.
#     {"c3_version": 2, "c3_file": "bridge.bin",
#      "c6_version": 2, "c6_file": "commissioner.bin"}
#   firmware/bridge.bin       (C3 / Bridge build)
#   firmware/commissioner.bin (C6 / Commissioner build)
FIRMWARE_DIR = os.path.join(HERE, "firmware")
os.makedirs(FIRMWARE_DIR, exist_ok=True)


@app.get("/firmware/manifest.json")
def firmware_manifest():
    path = os.path.join(FIRMWARE_DIR, "manifest.json")
    if os.path.exists(path):
        return FileResponse(path, media_type="application/json")
    return {"c3_version": 0, "c6_version": 0}   # nothing published yet


app.mount("/firmware", StaticFiles(directory=FIRMWARE_DIR), name="firmware")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _cli() -> None:
    global SERIAL_PORT, SERIAL_BAUD, LISTEN_PORT, DISCOVERY_URL, NODE_IP
    p = argparse.ArgumentParser(description="HVAC display / forwarding node")
    p.add_argument("--serial", help="serial port e.g. /dev/ttyACM0")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--port", type=int, default=LISTEN_PORT)
    p.add_argument("--discovery", default=DISCOVERY_URL)
    p.add_argument("--ip", default=NODE_IP,
                   help="LAN ip to advertise (use if auto-detect picks the wrong NIC)")
    args = p.parse_args()

    SERIAL_PORT = args.serial
    SERIAL_BAUD = args.baud
    LISTEN_PORT = args.port
    DISCOVERY_URL = args.discovery
    NODE_IP = args.ip

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=LISTEN_PORT)


if __name__ == "__main__":
    _cli()
