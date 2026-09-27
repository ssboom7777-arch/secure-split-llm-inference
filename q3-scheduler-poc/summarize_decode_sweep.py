from __future__ import annotations

import argparse
import json
from pathlib import Path


parser = argparse.ArgumentParser()
parser.add_argument("--baseline", type=Path, required=True)
parser.add_argument("--input", action="append", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
baseline = json.loads(args.baseline.read_text(encoding="utf-8-sig"))
base_texts = [x["text"] for x in baseline["requests"]]
base_client = baseline["client"]
rows = []
for path in args.input:
    run = json.loads(path.read_text(encoding="utf-8-sig"))
    client, cloud = run["client"], run["cloud"]
    concurrency = run["configuration"]["concurrency"]
    rows.append({
        "concurrency": concurrency,
        "outputs_identical_to_pipeline32": [x["text"] for x in run["requests"]] == base_texts,
        "wall_seconds": client["wall_seconds"],
        "request_throughput": client["requests_per_second"],
        "token_throughput": client["generated_steps_per_second"],
        "cloud_utilization": cloud["busy_seconds"] / client["wall_seconds"],
        "ttft_p95_ms": client["ttft_p95_seconds"] * 1000.0,
        "tpot_p95_ms": client["tpot_p95_seconds"] * 1000.0,
        "latency_p95_seconds": client["latency_p95_seconds"],
        "slot_seconds_per_1k_tokens": client["wall_seconds"] / client["generated_steps"] * 1000.0,
        "meets_tpot_100ms": client["tpot_p95_seconds"] <= 0.100,
    })
eligible = [x for x in rows if x["meets_tpot_100ms"]]
summary = {
    "baseline_pipeline_concurrency_32": {
        "wall_seconds": base_client["wall_seconds"],
        "token_throughput": base_client["generated_steps_per_second"],
        "tpot_p95_ms": base_client["tpot_p95_seconds"] * 1000.0,
    },
    "decode_priority_sweep": rows,
    "recommended": max(eligible, key=lambda x: x["token_throughput"]) if eligible else None,
}
args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
