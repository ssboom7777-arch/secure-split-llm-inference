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
        url, data=data, headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=900) as response:
        return json.loads(response.read().decode("utf-8"))


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(math.ceil(p * len(ordered)) - 1, len(ordered) - 1)]


def make_schedule(duration: float, users: int, mean_interval: float, seed: int) -> list[dict]:
    rng = random.Random(seed)
    sizes = [("short", 24, 0.60), ("medium", 96, 0.30), ("long", 384, 0.10)]
    output_lengths = [(4, 0.30), (8, 0.50), (16, 0.20)]
    events = []
    for user_id in range(users):
        at = rng.expovariate(1.0 / mean_interval)
        sequence = 0
        while at < duration:
            label, words, _ = rng.choices(sizes, weights=[x[2] for x in sizes], k=1)[0]
            max_tokens = rng.choices(
                [x[0] for x in output_lengths], weights=[x[1] for x in output_lengths], k=1
            )[0]
            payload = (f"User {user_id} request {sequence}: summarize the following enterprise note. "
                       + "revenue inventory customer forecast risk " * words)
            events.append({
                "id": len(events), "user_id": user_id, "user_sequence": sequence,
                "scheduled_at_seconds": at, "size_class": label, "prompt_words": words,
                "prompt": payload, "max_new_tokens": max_tokens,
            })
            sequence += 1
            at += rng.expovariate(1.0 / mean_interval)
    events.sort(key=lambda x: (x["scheduled_at_seconds"], x["user_id"]))
    for index, event in enumerate(events):
        event["id"] = index
    return events


def summarize(results: list[dict], wall: float, duration: float) -> dict:
    latencies = [x["latency_seconds"] for x in results]
    ttfts = [x["timing"]["ttft_seconds"] for x in results]
    tpots = [x["timing"]["mean_tpot_seconds"] for x in results if x["timing"]["mean_tpot_seconds"] > 0]
    arrival_lag = [x["submitted_at_seconds"] - x["scheduled_at_seconds"] for x in results]
    steps = sum(x["generated_steps"] for x in results)
    return {
        "scheduled_duration_seconds": duration,
        "wall_seconds_including_drain": wall,
        "request_count": len(results),
        "generated_steps": steps,
        "requests_per_second": len(results) / wall,
        "generated_steps_per_second": steps / wall,
        "latency_mean_seconds": statistics.mean(latencies),
        "latency_p50_seconds": percentile(latencies, 0.50),
        "latency_p95_seconds": percentile(latencies, 0.95),
        "latency_p99_seconds": percentile(latencies, 0.99),
        "ttft_p95_seconds": percentile(ttfts, 0.95),
        "tpot_p95_seconds": percentile(tpots, 0.95) if tpots else 0.0,
        "arrival_dispatch_lag_p95_seconds": percentile(arrival_lag, 0.95),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--make-schedule", action="store_true")
    parser.add_argument("--schedule", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=300.0)
    parser.add_argument("--users", type=int, default=5)
    parser.add_argument("--mean-user-interval", type=float, default=8.0)
    parser.add_argument("--seed", type=int, default=20260925)
    parser.add_argument("--enterprise-url", action="append")
    parser.add_argument("--cloud-url")
    parser.add_argument("--label")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.make_schedule:
        events = make_schedule(args.duration, args.users, args.mean_user_interval, args.seed)
        doc = {
            "assumptions": {
                "duration_seconds": args.duration, "users": args.users,
                "mean_user_interval_seconds": args.mean_user_interval, "arrival": "per-user exponential",
                "prompt_mix": {"short_24_words": 0.60, "medium_96_words": 0.30, "long_384_words": 0.10},
                "max_new_tokens_mix": {"4": 0.30, "8": 0.50, "16": 0.20}, "seed": args.seed,
            },
            "events": events,
        }
        args.schedule.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"schedule: {len(events)} requests")
        return

    if not all((args.enterprise_url, args.cloud_url, args.label, args.output)):
        parser.error("run mode requires enterprise/cloud URLs, label, and output")
    schedule_doc = json.loads(args.schedule.read_text(encoding="utf-8"))
    events = schedule_doc["events"]
    request_json(args.cloud_url + "/metrics/reset", {})
    origin = time.perf_counter()

    def run_one(event: dict, submitted: float) -> dict:
        started = time.perf_counter()
        enterprise_url = args.enterprise_url[event["user_id"] % len(args.enterprise_url)]
        response = request_json(enterprise_url + "/generate", {
            "prompt": event["prompt"], "max_new_tokens": event["max_new_tokens"], "stream": False,
        })
        finished = time.perf_counter()
        return {
            **{k: v for k, v in event.items() if k != "prompt"},
            "submitted_at_seconds": submitted, "completed_at_seconds": finished - origin,
            "latency_seconds": finished - started, **response,
        }

    futures = []
    with ThreadPoolExecutor(max_workers=64) as pool:
        for event in events:
            target = origin + event["scheduled_at_seconds"]
            remaining = target - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
            submitted = time.perf_counter() - origin
            futures.append(pool.submit(run_one, event, submitted))
        results = [future.result() for future in as_completed(futures)]
    wall = time.perf_counter() - origin
    results.sort(key=lambda x: x["id"])
    output = {
        "label": args.label, "schedule_assumptions": schedule_doc["assumptions"],
        "client": summarize(results, wall, args.duration),
        "cloud": request_json(args.cloud_url + "/metrics"), "requests": results,
    }
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output["client"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
