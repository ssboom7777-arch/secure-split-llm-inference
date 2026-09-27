from __future__ import annotations

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    runs = [json.loads(path.read_text(encoding="utf-8")) for path in args.input]
    baseline = runs[0]
    base_client = baseline["client"]
    base_wall_per_request = base_client["wall_seconds"] / baseline["configuration"]["requests"]
    base_texts = [item["text"] for item in baseline["requests"]]

    rows = []
    for run in runs:
        client, cloud = run["client"], run["cloud"]
        request_count = run["configuration"]["requests"]
        wall_per_request = client["wall_seconds"] / request_count
        rows.append({
            "label": run["label"],
            "outputs_identical_to_serial": [item["text"] for item in run["requests"]] == base_texts,
            "wall_seconds": client["wall_seconds"],
            "requests_per_second": client["requests_per_second"],
            "generated_steps_per_second": client["generated_steps_per_second"],
            "ttft_p95_seconds": client["ttft_p95_seconds"],
            "tpot_p95_seconds": client["tpot_p95_seconds"],
            "latency_p95_seconds": client["latency_p95_seconds"],
            "cloud_busy_seconds": cloud["busy_seconds"],
            "cloud_busy_ratio": cloud["busy_ratio"],
            "prefill_queue_wait_ms": cloud["prefill_average_queue_wait_ms"],
            "decode_queue_wait_ms": cloud["decode_average_queue_wait_ms"],
            "forward_batches": cloud["forward_batches"],
            "average_batch_size": cloud["average_batch_size"],
            "relative_cost_per_request": wall_per_request / base_wall_per_request,
            "cost_reduction_vs_serial": 1.0 - wall_per_request / base_wall_per_request,
        })

    summary = {
        "method": {
            "workload": "same requests, prompts, generated-token limit, model, and 500 km / 10 Gbps link",
            "cost_proxy": "wall-clock GPU instance occupancy per completed request",
            "cost_formula": "(wall_seconds / completed_requests) / serial_baseline",
            "warning": "busy_seconds is used for utilization diagnostics, not cloud billing cost",
        },
        "serial_baseline": rows[0]["label"],
        "experiments": rows,
    }
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
