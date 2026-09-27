from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class SharedDuplexLink:
    def __init__(self, bandwidth_gbps: float, propagation_ms: float):
        self.bandwidth_gbps = bandwidth_gbps
        self.propagation_seconds = propagation_ms / 1000.0
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            now = time.perf_counter()
            self.started_at = now
            self.next_available = {"uplink": now, "downlink": now}
            self.bytes = {"uplink": 0, "downlink": 0}
            self.transfers = {"uplink": 0, "downlink": 0}
            self.queue_wait = {"uplink": 0.0, "downlink": 0.0}
            self.serialization = {"uplink": 0.0, "downlink": 0.0}

    def transfer(self, direction: str, raw_bytes: int) -> dict:
        if direction not in self.next_available:
            raise ValueError(f"invalid direction: {direction}")
        serialization = raw_bytes * 8.0 / (self.bandwidth_gbps * 1e9)
        with self.lock:
            now = time.perf_counter()
            start = max(now, self.next_available[direction])
            wait = start - now
            self.next_available[direction] = start + serialization
            self.bytes[direction] += raw_bytes
            self.transfers[direction] += 1
            self.queue_wait[direction] += wait
            self.serialization[direction] += serialization
        delay = wait + serialization + self.propagation_seconds
        if delay > 0:
            time.sleep(delay)
        return {"delay_seconds": delay, "queue_wait_seconds": wait, "serialization_seconds": serialization}

    def metrics(self):
        with self.lock:
            wall = max(time.perf_counter() - self.started_at, 1e-9)
            return {
                "wall_seconds": wall,
                "bandwidth_gbps_per_direction": self.bandwidth_gbps,
                "propagation_ms_one_way": self.propagation_seconds * 1000.0,
                "directions": {
                    direction: {
                        "bytes": self.bytes[direction],
                        "transfers": self.transfers[direction],
                        "serialization_seconds": self.serialization[direction],
                        "queue_wait_seconds": self.queue_wait[direction],
                        "average_queue_wait_ms": (
                            self.queue_wait[direction] / self.transfers[direction] * 1000.0
                            if self.transfers[direction] else 0.0
                        ),
                        "link_utilization": min(self.serialization[direction] / wall, 1.0),
                    }
                    for direction in ("uplink", "downlink")
                },
            }


LINK: SharedDuplexLink


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def reply(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"status": "ok", "service": "shared-duplex-link"})
        elif self.path == "/metrics":
            self.reply(200, LINK.metrics())
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if self.path == "/transfer":
                self.reply(200, LINK.transfer(payload["direction"], int(payload["bytes"])))
            elif self.path == "/metrics/reset":
                LINK.reset()
                self.reply(200, {"reset": True})
            else:
                self.reply(404, {"error": "not found"})
        except Exception as exc:
            self.reply(500, {"error": f"{type(exc).__name__}: {exc}"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8399)
    parser.add_argument("--bandwidth-gbps", type=float, default=10.0)
    parser.add_argument("--propagation-ms", type=float, default=2.5)
    args = parser.parse_args()
    global LINK
    LINK = SharedDuplexLink(args.bandwidth_gbps, args.propagation_ms)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
