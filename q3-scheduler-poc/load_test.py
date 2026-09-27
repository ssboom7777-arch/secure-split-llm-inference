from __future__ import annotations

import argparse
import json
import math
import statistics
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def request_json(url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read().decode("utf-8"))


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(math.ceil(p * len(ordered)) - 1, len(ordered) - 1)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--enterprise-url", required=True)
    parser.add_argument("--cloud-url", required=True)
    parser.add_argument("--requests", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--max-new-tokens", type=int, default=8)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    request_json(args.cloud_url + "/metrics/reset", {})
    barrier = threading.Barrier(args.concurrency)

    def run_one(index: int) -> dict:
        # Identical prompt lengths keep requests batch-compatible; content varies.
        prompt = f"Say hello number {index:02d}."
        barrier.wait()
        started = time.perf_counter()
        result = request_json(args.enterprise_url + "/generate", {
            "prompt": prompt,
            "max_new_tokens": args.max_new_tokens,
            "stream": False,
        })
        return {"index": index, "latency_seconds": time.perf_counter() - started, **result}

    started = time.perf_counter()
    results = []
    # One wave is intentional: it exposes whether the scheduler coalesces requests.
    for offset in range(0, args.requests, args.concurrency):
        wave = min(args.concurrency, args.requests - offset)
        barrier = threading.Barrier(wave)
        with ThreadPoolExecutor(max_workers=wave) as pool:
            results.extend(pool.map(run_one, range(offset, offset + wave)))
    elapsed = time.perf_counter() - started
    latencies = [item["latency_seconds"] for item in results]
    ttfts = [item["timing"]["ttft_seconds"] for item in results]
    tpots = [item["timing"]["mean_tpot_seconds"] for item in results if item["timing"]["mean_tpot_seconds"] > 0]
    steps = sum(item.get("generated_steps", 0) for item in results)
    output = {
        "label": args.label,
        "configuration": {
            "requests": args.requests,
            "concurrency": args.concurrency,
            "max_new_tokens": args.max_new_tokens,
        },
        "client": {
            "wall_seconds": elapsed,
            "requests_per_second": args.requests / elapsed,
            "generated_steps": steps,
            "generated_steps_per_second": steps / elapsed,
            "latency_mean_seconds": statistics.mean(latencies),
            "latency_p50_seconds": percentile(latencies, 0.50),
            "latency_p95_seconds": percentile(latencies, 0.95),
            "ttft_mean_seconds": statistics.mean(ttfts),
            "ttft_p95_seconds": percentile(ttfts, 0.95),
            "tpot_mean_seconds": statistics.mean(tpots) if tpots else 0.0,
            "tpot_p95_seconds": percentile(tpots, 0.95) if tpots else 0.0,
        },
        "cloud": request_json(args.cloud_url + "/metrics"),
        "requests": sorted(results, key=lambda item: item["index"]),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output["client"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
