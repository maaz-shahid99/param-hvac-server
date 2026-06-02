"""
Sensor simulator
=================

Pretends to be the field sensors so you can exercise the whole pipeline with no
hardware. Each simulated sensor:

  1. Heart-beats the discovery server  -> POST /register/sensor   (presence)
  2. Asks the discovery server for the forwarding node's LAN ip -> GET /discover
  3. Streams probe readings straight to that node (P2P)         -> POST /ingest
     ... and also mirrors a copy to the discovery server         -> POST /data

It models the real layout: 12 boxes, 2 sensors each (slot A / B), 8 probes per
sensor (change --probes to reconfigure).

Run (after starting discovery_server.py and display_node.py):
    python sensor_sim.py
    python sensor_sim.py --discovery http://localhost:8000 --probes 8 --period 2
"""

from __future__ import annotations

import argparse
import math
import random
import time

import httpx


def make_probes(base: float, n: int, t: float, phase: float) -> list[float]:
    """A gentle sine drift plus noise so the dashboard looks alive."""
    return [
        round(base + 6 * math.sin(t / 10 + phase + i) + random.uniform(-1.5, 1.5), 1)
        for i in range(n)
    ]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--discovery", default="http://localhost:8000")
    p.add_argument("--node", default="", help="force forwarder url, skip discover")
    p.add_argument("--boxes", type=int, default=12)
    p.add_argument("--probes", type=int, default=8)
    p.add_argument("--period", type=float, default=2.0)
    args = p.parse_args()

    client = httpx.Client(timeout=5)
    # one base temperature per box so boxes look distinct
    base = {b: random.uniform(28, 55) for b in range(1, args.boxes + 1)}
    t0 = time.time()
    last_discover = 0.0
    node_url = args.node

    print(f"simulating {args.boxes} boxes x 2 sensors x {args.probes} probes")
    while True:
        t = time.time() - t0

        # refresh the forwarder endpoint from discovery every ~10s
        if not args.node and time.time() - last_discover > 10:
            last_discover = time.time()
            try:
                r = client.get(f"{args.discovery}/discover").json()
                fwd = r.get("forwarders") or []
                if fwd:
                    node_url = f"http://{fwd[0]['local_ip']}:{fwd[0]['port']}"
                    print(f"[discover] forwarding node -> {node_url}")
                else:
                    print("[discover] no forwarding node yet")
            except Exception as exc:                   # noqa: BLE001
                print(f"[discover] error: {exc}")

        for b in range(1, args.boxes + 1):
            for slot in ("A", "B"):
                sensor_id = f"box{b}-{slot}"
                phase = (b * 2 + (0 if slot == "A" else 1)) * 0.7
                probes = make_probes(base[b] + (0 if slot == "A" else 3),
                                     args.probes, t, phase)

                # 1) presence heartbeat -> discovery (means "internet is up")
                try:
                    client.post(f"{args.discovery}/register/sensor",
                                json={"sensor_id": sensor_id,
                                      "meta": {"box": b, "slot": slot}})
                except Exception:
                    pass

                # 2) P2P stream to the forwarding node
                if node_url:
                    try:
                        client.post(f"{node_url}/ingest",
                                    json={"sensor_id": sensor_id, "probes": probes,
                                          "box": b, "slot": slot})
                    except Exception as exc:           # noqa: BLE001
                        print(f"[ingest] {sensor_id} -> {node_url} failed: {exc}")

                # 3) mirror a copy to the discovery server (for mobile routing)
                try:
                    client.post(f"{args.discovery}/data",
                                json={"sensor_id": sensor_id,
                                      "payload": {"box": b, "slot": slot,
                                                  "probes": probes}})
                except Exception:
                    pass

        print(f"t={t:6.1f}s  pushed {args.boxes * 2} readings")
        time.sleep(args.period)


if __name__ == "__main__":
    main()
