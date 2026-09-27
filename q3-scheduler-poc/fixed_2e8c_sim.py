from __future__ import annotations

import argparse
import json
from pathlib import Path

from tokenizers import Tokenizer

from pd_capacity_sim import DTYPE_BYTES, HEAD_DIM, HIDDEN, KV_HEADS, LAYERS, percentile, simulate


def network_delay(raw_bytes: int, one_way_ms=2.5, bandwidth_gbps=10.0) -> float:
    return one_way_ms / 1000.0 + raw_bytes * 8.0 / (bandwidth_gbps * 1e9)


def strict_serial(events, token_lengths, prefill_services, decode_service):
    available = 0.0
    busy = 0.0
    ttfts, tpots, latencies = [], [], []
    for item in events:
        arrival = item["scheduled_at_seconds"]
        start = max(arrival, available)
        seq = token_lengths[item["id"]]
        up_prefill = network_delay(seq * HIDDEN * DTYPE_BYTES)
        down_prefill = network_delay(seq * HIDDEN * DTYPE_BYTES)
        prefill = prefill_services[item["id"]]
        busy += prefill
        first_token = start + up_prefill + prefill + down_prefill
        token_times = [first_token]
        current = first_token
        for index in range(item["max_new_tokens"]):
            current += network_delay(HIDDEN * DTYPE_BYTES) + decode_service + network_delay(HIDDEN * DTYPE_BYTES)
            busy += decode_service
            if index < item["max_new_tokens"] - 1:
                token_times.append(current)
        available = current
        ttfts.append(first_token - arrival)
        tpots.extend(b - a for a, b in zip(token_times, token_times[1:]))
        latencies.append(available - arrival)
    wall = available - min(item["scheduled_at_seconds"] for item in events)
    return {
        "name": "serial-unified-8c", "enterprise_cards": 2, "cloud_cards": 8,
        "cloud_layout": "8-card unified pool; one request admitted end-to-end at a time",
        "wall_seconds": wall, "throughput_requests_per_second": len(events) / wall,
        "ttft_p95_seconds": percentile(ttfts, .95), "tpot_p95_seconds": percentile(tpots, .95),
        "latency_p95_seconds": percentile(latencies, .95),
        "cloud_utilization": busy / wall, "cloud_card_seconds": 8 * wall,
        "kv_transfer_gib": 0.0, "kv_transfer_serialized_seconds": 0.0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--schedule", type=Path, required=True)
    parser.add_argument("--serial-result", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    schedule = json.loads(args.schedule.read_text(encoding="utf-8"))
    events = schedule["events"]
    measured = json.loads(args.serial_result.read_text(encoding="utf-8"))["cloud"]
    tokenizer = Tokenizer.from_file(str(args.tokenizer))
    token_lengths, weights = {}, {}
    for item in events:
        formatted = f"<|im_start|>user\n{item['prompt']}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        count = len(tokenizer.encode(formatted, add_special_tokens=False).ids)
        token_lengths[item["id"]] = count
        weights[item["id"]] = count + 0.0008 * count * count
    scale = measured["prefill_busy_seconds"] / sum(weights.values())
    prefill_services = {rid: weight * scale for rid, weight in weights.items()}
    decode_service = measured["decode_busy_seconds"] / measured["decode_jobs"]

    serial = strict_serial(events, token_lengths, prefill_services, decode_service)
    pipeline = simulate(
        "pipeline-unified-8c", events, token_lengths, prefill_services, decode_service,
        0, 0, 1, 2.5, 10.0, 100.0, pool_cards={"u": 8},
    )
    separated_6p2d = simulate(
        "pipeline-separated-6P2D", events, token_lengths, prefill_services, decode_service,
        1, 1, 0, 2.5, 10.0, 100.0,
        prefill_service_factor=8 / 6, decode_service_factor=8 / 2,
        pool_cards={"p": 6, "d": 2},
    )
    separated_5p3d = simulate(
        "pipeline-separated-5P3D", events, token_lengths, prefill_services, decode_service,
        1, 1, 0, 2.5, 10.0, 100.0,
        prefill_service_factor=8 / 5, decode_service_factor=8 / 3,
        pool_cards={"p": 5, "d": 3},
    )
    results = [serial, pipeline, separated_6p2d, separated_5p3d]
    for result in results[1:]:
        ratios = result["pool_busy_ratio"]
        cards = result["workers"]
        if "u" in ratios:
            result["cloud_utilization"] = ratios["u"]
            result["enterprise_cards"], result["cloud_cards"] = 2, 8
            result["cloud_layout"] = "8-card unified pool; request-level pipeline"
        else:
            p_cards = 6 if "6P2D" in result["name"] else 5
            d_cards = 8 - p_cards
            result["cloud_utilization"] = (
                ratios["p"] * p_cards + ratios["d"] * d_cards
            ) / 8
            result["enterprise_cards"], result["cloud_cards"] = 2, 8
            result["cloud_layout"] = f"{p_cards} Prefill cards + {d_cards} Decode cards"
    base_cost = serial["cloud_card_seconds"]
    for result in results:
        result["relative_cost"] = result["cloud_card_seconds"] / base_cost
        result["cost_reduction_vs_serial"] = 1 - result["relative_cost"]

    output = {
        "status": "calibrated local simulation, not 8-GPU hardware measurement",
        "fixed_physical_configuration": {"enterprise_cards": 2, "cloud_cards": 8},
        "workload": schedule["assumptions"],
        "calibration": {
            "source": "real MiniMind 5-user sustained execution on this laptop",
            "prefill_busy_seconds": measured["prefill_busy_seconds"],
            "decode_busy_seconds": measured["decode_busy_seconds"],
            "request_count": len(events), "cloud_internal_kv_bandwidth_gbps": 100.0,
            "pd_scaling_assumption": "ideal inverse scaling with assigned card count",
        },
        "cost_formula": "8 cloud cards * wall time; card-hour price cancels in relative comparison",
        "results": results,
    }
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
