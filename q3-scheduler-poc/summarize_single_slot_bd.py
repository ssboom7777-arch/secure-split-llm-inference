from __future__ import annotations

import argparse
import json
from pathlib import Path


parser = argparse.ArgumentParser()
parser.add_argument("baseline", type=Path)
parser.add_argument("pipeline", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
b = json.loads(args.baseline.read_text(encoding="utf-8-sig"))
d = json.loads(args.pipeline.read_text(encoding="utf-8-sig"))


def row(run):
    client, cloud, link = run["client"], run["cloud"], run["shared_link"]
    wall = client["wall_seconds"]
    busy = cloud["busy_seconds"]
    output_tokens = client["generated_steps"]
    return {
        "wall_seconds": wall,
        "request_throughput": client["requests_per_second"],
        "token_throughput": client["generated_steps_per_second"],
        "cloud_busy_seconds": busy,
        "cloud_utilization": busy / wall,
        "non_compute_gap_seconds": max(wall - busy, 0.0),
        "non_compute_gap_ratio": max(wall - busy, 0.0) / wall,
        "latency_p50_seconds": client["latency_p50_seconds"],
        "latency_p95_seconds": client["latency_p95_seconds"],
        "ttft_p95_seconds": client["ttft_p95_seconds"],
        "tpot_p95_seconds": client["tpot_p95_seconds"],
        "cloud_slot_seconds_per_1k_tokens": wall / output_tokens * 1000.0,
        "uplink_utilization": link["directions"]["uplink"]["link_utilization"],
        "downlink_utilization": link["directions"]["downlink"]["link_utilization"],
        "uplink_queue_wait_seconds": link["directions"]["uplink"]["queue_wait_seconds"],
        "downlink_queue_wait_seconds": link["directions"]["downlink"]["queue_wait_seconds"],
    }


br, dr = row(b), row(d)
baseline_text = [item["text"] for item in b["requests"]]
pipeline_text = [item["text"] for item in d["requests"]]
summary = {
    "experiment": "single real MiniMind cloud execution slot; B serial vs D request pipeline",
    "outputs_identical": baseline_text == pipeline_text,
    "baseline_B": br,
    "pipeline_D": dr,
    "improvement": {
        "wall_time_reduction": 1.0 - dr["wall_seconds"] / br["wall_seconds"],
        "request_throughput_speedup": dr["request_throughput"] / br["request_throughput"],
        "token_throughput_speedup": dr["token_throughput"] / br["token_throughput"],
        "cloud_utilization_delta_points": (dr["cloud_utilization"] - br["cloud_utilization"]) * 100.0,
        "cost_per_1k_tokens_reduction": 1.0 - dr["cloud_slot_seconds_per_1k_tokens"] / br["cloud_slot_seconds_per_1k_tokens"],
    },
    "note": "non_compute_gap includes network RTT, enterprise LM-head/sampling, HTTP and scheduler overhead; it is not pure network time.",
}
args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
