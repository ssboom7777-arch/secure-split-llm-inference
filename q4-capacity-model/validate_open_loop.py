from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def request_json(url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=900) as response:
        return json.loads(response.read().decode("utf-8"))


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(math.ceil(p * len(ordered)) - 1, len(ordered) - 1)]


def make_schedule(rate: float, duration: float, seed: int) -> list[dict]:
    rng = random.Random(seed)
    # Conditional on an exact event count, Poisson arrival times in an interval
    # are sorted uniform samples. This preserves randomized arrivals while making
    # the offered QPS of every finite-duration test level exact and comparable.
    count = max(1, round(rate * duration))
    arrivals = sorted(rng.uniform(0.0, duration) for _ in range(count))
    events = []
    for index, at in enumerate(arrivals):
        events.append({
            "id": index,
            "scheduled_at_seconds": at,
            "prompt": f"Say hello number {index:04d}.",
        })
    return events


def main() -> None:
    parser = argparse.ArgumentParser(description="Open-loop end-to-end capacity validation")
    parser.add_argument("--enterprise-url", required=True)
    parser.add_argument("--cloud-url", required=True)
    parser.add_argument("--offered-qps", type=float, required=True)
    parser.add_argument("--duration", type=float, default=45.0)
    parser.add_argument("--max-new-tokens", type=int, default=64)
    parser.add_argument("--seed", type=int, default=20260926)
    parser.add_argument("--ttft-slo", type=float, default=3.0)
    parser.add_argument("--tpot-slo", type=float, default=0.1)
    parser.add_argument("--max-drain-seconds", type=float, default=10.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    events = make_schedule(args.offered_qps, args.duration, args.seed)
    if not events:
        raise RuntimeError("No arrivals generated; increase duration or offered QPS")
    request_json(args.cloud_url + "/metrics/reset", {})
    origin = time.perf_counter()

    def run_one(event: dict, submitted: float) -> dict:
        started = time.perf_counter()
        response = request_json(args.enterprise_url + "/generate", {
            "prompt": event["prompt"],
            "max_new_tokens": args.max_new_tokens,
            "stream": False,
        })
        return {
            "id": event["id"],
            "scheduled_at_seconds": event["scheduled_at_seconds"],
            "submitted_at_seconds": submitted,
            "latency_seconds": time.perf_counter() - started,
            **response,
        }

    futures = []
    with ThreadPoolExecutor(max_workers=128) as pool:
        for event in events:
            remaining = origin + event["scheduled_at_seconds"] - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
            submitted = time.perf_counter() - origin
            futures.append(pool.submit(run_one, event, submitted))
        results = [future.result() for future in as_completed(futures)]

    wall = time.perf_counter() - origin
    results.sort(key=lambda item: item["id"])
    ttfts = [item["timing"]["ttft_seconds"] for item in results]
    tpots = [item["timing"]["mean_tpot_seconds"] for item in results if item["timing"]["mean_tpot_seconds"] > 0]
    dispatch_lags = [item["submitted_at_seconds"] - item["scheduled_at_seconds"] for item in results]
    generated = sum(item["generated_steps"] for item in results)
    drain = max(0.0, wall - args.duration)
    measured = {
        "scheduled_requests": len(events),
        "actual_offered_qps": len(events) / args.duration,
        "completed_qps_including_drain": len(results) / wall,
        "token_throughput": generated / wall,
        "wall_seconds_including_drain": wall,
        "drain_seconds": drain,
        "ttft_p95_seconds": percentile(ttfts, 0.95),
        "tpot_p95_seconds": percentile(tpots, 0.95) if tpots else 0.0,
        "dispatch_lag_p95_seconds": percentile(dispatch_lags, 0.95),
    }
    measured["slo_pass"] = (
        measured["ttft_p95_seconds"] <= args.ttft_slo
        and measured["tpot_p95_seconds"] <= args.tpot_slo
    )
    measured["stable"] = drain <= args.max_drain_seconds + 0.01 and measured["dispatch_lag_p95_seconds"] <= 0.1
    measured["capacity_pass"] = measured["slo_pass"] and measured["stable"]
    output = {
        "experiment": "Q4 open-loop predict-vs-measure validation",
        "configuration": {
            "offered_qps": args.offered_qps,
            "duration_seconds": args.duration,
            "arrival_process": "Poisson",
            "seed": args.seed,
            "max_new_tokens": args.max_new_tokens,
            "ttft_slo_seconds": args.ttft_slo,
            "tpot_slo_seconds": args.tpot_slo,
            "max_drain_seconds": args.max_drain_seconds,
        },
        "measured": measured,
        "cloud": request_json(args.cloud_url + "/metrics"),
        "requests": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(measured, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
