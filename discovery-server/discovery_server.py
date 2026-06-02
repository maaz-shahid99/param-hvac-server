"""
Discovery Server
================

Runs on a remote / cloud host (somewhere with reliable internet, NOT on the
same LAN as the sensors). Its jobs:

  1. Be the rendezvous point every device first connects to:
       - sensors            -> POST /register/sensor   (heartbeat)
       - the forwarding node -> POST /register/forwarder (announces its LAN ip)
       - fetching device     -> GET  /discover           (learns the LAN ip)
       - mobile devices      -> GET  /mobile/poll         (receive routed data)

  2. Internet-presence watchdog: if a sensor is connected we assume the site
     has internet. The moment every sensor stops heart-beating we assume the
     internet at the site went down and we e-mail an alert (this server lives
     elsewhere, so it still has connectivity to send the mail).

  3. Hand out the *local* ip of the forwarding node so sensors / the fetching
     device can talk to it peer-to-peer directly (the heavy data never has to
     round-trip through here).

  4. Keep a little bit of state in sqlite3 and forward "fetch data" events to
     whichever sensor is connected, via a long-poll so nothing busy-spins.

Run:
    pip install -r requirements.txt
    uvicorn discovery_server:app --host 0.0.0.0 --port 8000

Email is configured through environment variables (see .env.example).
"""

from __future__ import annotations

import asyncio
import os
import smtplib
import sqlite3
import time
from contextlib import asynccontextmanager
from email.message import EmailMessage
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

DB_PATH = os.environ.get("DISCOVERY_DB", "discovery.db")

# A sensor is considered "gone" if we have not heard from it for this long.
HEARTBEAT_TIMEOUT = float(os.environ.get("HEARTBEAT_TIMEOUT", "30"))   # seconds
# How often the watchdog wakes up to check presence.
WATCHDOG_INTERVAL = float(os.environ.get("WATCHDOG_INTERVAL", "5"))    # seconds
# How long /poll waits before returning "nothing yet" (keeps CPU idle).
LONG_POLL_TIMEOUT = float(os.environ.get("LONG_POLL_TIMEOUT", "25"))   # seconds

# Email (SMTP) — leave SMTP_HOST empty to just log alerts instead of sending.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
ALERT_FROM = os.environ.get("ALERT_FROM", SMTP_USER or "alerts@example.com")
ALERT_TO = os.environ.get("ALERT_TO", "")          # comma separated
SITE_NAME = os.environ.get("SITE_NAME", "HVAC site")


# --------------------------------------------------------------------------- #
# SQLite helpers                                                              #
# --------------------------------------------------------------------------- #

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sensors (
                sensor_id   TEXT PRIMARY KEY,
                last_seen   REAL,
                meta        TEXT
            );

            CREATE TABLE IF NOT EXISTS forwarders (
                node_id     TEXT PRIMARY KEY,
                local_ip    TEXT,
                port        INTEGER,
                last_seen   REAL
            );

            CREATE TABLE IF NOT EXISTS readings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                sensor_id   TEXT,
                ts          REAL,
                payload     TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);
            """
        )


# --------------------------------------------------------------------------- #
# In-memory pub/sub for long-polling (no busy loops)                          #
# --------------------------------------------------------------------------- #

class Broker:
    """A tiny per-topic fan-out. Each waiter gets its own asyncio.Queue, so a
    long-poll simply `await`s the queue with a timeout and the event loop puts
    the coroutine to sleep — zero CPU until something is published."""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, topic: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(topic, set()).add(q)
        return q

    def unsubscribe(self, topic: str, q: asyncio.Queue) -> None:
        if topic in self._subs:
            self._subs[topic].discard(q)
            if not self._subs[topic]:
                del self._subs[topic]

    def publish(self, topic: str, message: Any) -> int:
        subs = self._subs.get(topic, set())
        for q in subs:
            q.put_nowait(message)
        return len(subs)


broker = Broker()


# --------------------------------------------------------------------------- #
# Presence / alert state                                                       #
# --------------------------------------------------------------------------- #

class Presence:
    """Tracks whether the site looks online (any sensor heart-beating) and
    makes sure we only e-mail once per outage, not every watchdog tick."""

    online: bool = True          # optimistic until proven otherwise
    alerted_offline: bool = False
    last_change: float = time.time()


presence = Presence()


def _send_email(subject: str, body: str) -> None:
    """Best-effort SMTP send. Never raises into the caller."""
    recipients = [r.strip() for r in ALERT_TO.split(",") if r.strip()]
    if not SMTP_HOST or not recipients:
        print(f"[email:skipped] {subject} -> {recipients or 'no recipients'}")
        print(body)
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = ALERT_FROM
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            smtp.starttls()
            if SMTP_USER:
                smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(msg)
        print(f"[email:sent] {subject}")
    except Exception as exc:                      # noqa: BLE001
        print(f"[email:error] {exc}")


def active_sensors(now: float | None = None) -> list[sqlite3.Row]:
    now = now or time.time()
    cutoff = now - HEARTBEAT_TIMEOUT
    with db() as conn:
        return conn.execute(
            "SELECT * FROM sensors WHERE last_seen >= ?", (cutoff,)
        ).fetchall()


async def watchdog() -> None:
    """Background task: flips online/offline and e-mails on the transition."""
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)
        now = time.time()
        any_alive = len(active_sensors(now)) > 0

        if any_alive and not presence.online:
            presence.online = True
            presence.alerted_offline = False
            presence.last_change = now
            print("[watchdog] site back ONLINE")
            _send_email(
                f"[{SITE_NAME}] back online",
                f"A sensor reconnected at {time.ctime(now)}. Internet is back.",
            )

        elif not any_alive and presence.online:
            presence.online = False
            presence.last_change = now
            print("[watchdog] site OFFLINE — no sensor heartbeats")
            if not presence.alerted_offline:
                presence.alerted_offline = True
                _send_email(
                    f"[{SITE_NAME}] OFFLINE — sensors unreachable",
                    "No sensor has reported in the last "
                    f"{HEARTBEAT_TIMEOUT:.0f}s as of {time.ctime(now)}.\n"
                    "We assume the site lost internet connectivity.",
                )


# --------------------------------------------------------------------------- #
# FastAPI app                                                                  #
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(watchdog())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="HVAC Discovery Server", lifespan=lifespan)


# ---- request models -------------------------------------------------------- #

class SensorBeat(BaseModel):
    sensor_id: str
    meta: dict[str, Any] = Field(default_factory=dict)


class ForwarderBeat(BaseModel):
    node_id: str
    local_ip: str
    port: int = 8001


class Reading(BaseModel):
    sensor_id: str
    payload: dict[str, Any]


class FetchEvent(BaseModel):
    sensor_id: str | None = None      # None -> broadcast to all sensors
    command: str = "fetch"
    args: dict[str, Any] = Field(default_factory=dict)


# ---- sensor presence ------------------------------------------------------- #

@app.post("/register/sensor")
def register_sensor(beat: SensorBeat):
    """A sensor checks in. Presence here == we believe the site has internet."""
    import json
    now = time.time()
    with db() as conn:
        conn.execute(
            "INSERT INTO sensors(sensor_id, last_seen, meta) VALUES(?,?,?) "
            "ON CONFLICT(sensor_id) DO UPDATE SET last_seen=?, meta=?",
            (beat.sensor_id, now, json.dumps(beat.meta), now, json.dumps(beat.meta)),
        )
    return {"ok": True, "server_time": now, "online": presence.online}


# ---- forwarding node presence --------------------------------------------- #

@app.post("/register/forwarder")
def register_forwarder(beat: ForwarderBeat):
    """The data-forwarding node (on the user's computer) announces its LAN ip
    so sensors/the fetching device can reach it peer-to-peer."""
    now = time.time()
    with db() as conn:
        conn.execute(
            "INSERT INTO forwarders(node_id, local_ip, port, last_seen) "
            "VALUES(?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET "
            "local_ip=?, port=?, last_seen=?",
            (beat.node_id, beat.local_ip, beat.port, now,
             beat.local_ip, beat.port, now),
        )
    return {"ok": True}


@app.get("/discover")
def discover():
    """Return the LAN endpoint(s) of currently-alive forwarding node(s). The
    sensors / fetching device use this to set up a direct P2P connection — the
    bulk data never flows through this server."""
    cutoff = time.time() - HEARTBEAT_TIMEOUT
    with db() as conn:
        rows = conn.execute(
            "SELECT node_id, local_ip, port, last_seen FROM forwarders "
            "WHERE last_seen >= ? ORDER BY last_seen DESC", (cutoff,)
        ).fetchall()
    return {"forwarders": [dict(r) for r in rows]}


# ---- data storage ---------------------------------------------------------- #

@app.post("/data")
def post_data(reading: Reading):
    """Some data is forwarded to the server — keep it in sqlite and fan it out
    to any mobile devices that are long-polling /mobile/poll."""
    import json
    now = time.time()
    with db() as conn:
        conn.execute(
            "INSERT INTO readings(sensor_id, ts, payload) VALUES(?,?,?)",
            (reading.sensor_id, now, json.dumps(reading.payload)),
        )
    broker.publish("mobile", {"sensor_id": reading.sensor_id,
                              "ts": now, "payload": reading.payload})
    return {"ok": True, "ts": now}


@app.get("/data")
def get_data(sensor_id: str | None = None, limit: int = 100):
    import json
    q = "SELECT sensor_id, ts, payload FROM readings"
    params: list[Any] = []
    if sensor_id:
        q += " WHERE sensor_id = ?"
        params.append(sensor_id)
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    with db() as conn:
        rows = conn.execute(q, params).fetchall()
    return {"readings": [
        {"sensor_id": r["sensor_id"], "ts": r["ts"],
         "payload": json.loads(r["payload"])}
        for r in rows
    ]}


# ---- fetch-data events (server -> sensor) --------------------------------- #

@app.post("/fetch")
def trigger_fetch(event: FetchEvent):
    """A 'fetch data' event arrives. Forward it to the connected sensor(s) that
    are parked on /poll."""
    topic = f"sensor:{event.sensor_id}" if event.sensor_id else "sensor:*"
    delivered = broker.publish(topic, event.model_dump())
    if event.sensor_id:                                  # also hit the wildcard
        delivered += broker.publish("sensor:*", event.model_dump())
    return {"ok": True, "delivered_to": delivered}


@app.get("/poll")
async def poll(sensor_id: str):
    """Long-poll for the given sensor. Blocks (without burning CPU) until a
    fetch event is published for it or the timeout elapses, then returns."""
    topic = f"sensor:{sensor_id}"
    q = broker.subscribe(topic)
    wild = broker.subscribe("sensor:*")
    try:
        getter = asyncio.ensure_future(_first_of(q, wild))
        msg = await asyncio.wait_for(getter, timeout=LONG_POLL_TIMEOUT)
        return {"event": msg}
    except asyncio.TimeoutError:
        return {"event": None}        # client should immediately poll again
    finally:
        broker.unsubscribe(topic, q)
        broker.unsubscribe("sensor:*", wild)


@app.get("/mobile/poll")
async def mobile_poll():
    """Mobile devices park here and receive routed readings as they arrive."""
    q = broker.subscribe("mobile")
    try:
        msg = await asyncio.wait_for(q.get(), timeout=LONG_POLL_TIMEOUT)
        return {"data": msg}
    except asyncio.TimeoutError:
        return {"data": None}
    finally:
        broker.unsubscribe("mobile", q)


async def _first_of(*queues: asyncio.Queue):
    """Return the first message available from any of the given queues."""
    tasks = [asyncio.ensure_future(q.get()) for q in queues]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    return done.pop().result()


# ---- status ---------------------------------------------------------------- #

@app.get("/status")
def status():
    now = time.time()
    sensors = active_sensors(now)
    with db() as conn:
        fwd = conn.execute(
            "SELECT node_id, local_ip, port, last_seen FROM forwarders "
            "WHERE last_seen >= ?", (now - HEARTBEAT_TIMEOUT,)
        ).fetchall()
        count = conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"]
    return {
        "online": presence.online,
        "active_sensors": [s["sensor_id"] for s in sensors],
        "forwarders": [dict(r) for r in fwd],
        "stored_readings": count,
        "server_time": now,
    }


@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": str(exc)})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
