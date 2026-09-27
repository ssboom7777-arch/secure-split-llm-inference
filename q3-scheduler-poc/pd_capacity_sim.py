from __future__ import annotations

import argparse
import heapq
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from tokenizers import Tokenizer


HIDDEN = 768
LAYERS = 8
KV_HEADS = 4
HEAD_DIM = 96
DTYPE_BYTES = 4


def percentile(values, p):
    values = sorted(values)
    return values[min(math.ceil(p * len(values)) - 1, len(values) - 1)]


@dataclass(order=True)
class Event:
    time: float
    sequence: int
    kind: str = field(compare=False)
    pool: str = field(compare=False)
    worker: int = field(compare=False, default=-1)
    request_id: int = field(compare=False, default=-1)
    phase: str = field(compare=False, default="prefill")
    decode_index: int = field(compare=False, default=0)


def simulate(name, events, token_lengths, prefill_services, decode_service, p_workers, d_workers,
             unified_workers, one_way_ms, bandwidth_gbps, cloud_bandwidth_gbps,
             prefill_service_factor=1.0, decode_service_factor=1.0, pool_cards=None):
    split = unified_workers == 0
    pools = ({"p": p_workers, "d": d_workers} if split else {"u": unified_workers})
    idle = {pool: list(range(count)) for pool, count in pools.items()}
    queues = {pool: [] for pool in pools}
    calendar = []
    sequence = 0
    state = {}
    busy = {pool: 0.0 for pool in pools}
    kv_bytes_total = 0
    kv_transfer_seconds = 0.0

    def net(seconds_bytes):
        return one_way_ms / 1000.0 + seconds_bytes * 8.0 / (bandwidth_gbps * 1e9)

    def push(time, kind, pool, worker=-1, rid=-1, phase="prefill", decode_index=0):
        nonlocal sequence
        heapq.heappush(calendar, Event(time, sequence, kind, pool, worker, rid, phase, decode_index))
        sequence += 1

    def dispatch(pool, now):
        while idle[pool] and queues[pool]:
            ready, _, rid, phase, decode_index = heapq.heappop(queues[pool])
            if ready > now:
                push(ready, "wake", pool)
                heapq.heappush(queues[pool], (ready, sequence, rid, phase, decode_index))
                return
            worker = idle[pool].pop()
            service = (prefill_services[rid] * prefill_service_factor
                       if phase == "prefill" else decode_service * decode_service_factor)
            busy[pool] += service
            state[rid][f"{phase}_queue_wait"] += now - ready
            push(now + service, "complete", pool, worker, rid, phase, decode_index)

    for item in events:
        rid = item["id"]
        state[rid] = {
            "arrival": item["scheduled_at_seconds"], "max_tokens": item["max_new_tokens"],
            "token_times": [], "done": None, "prefill_queue_wait": 0.0, "decode_queue_wait": 0.0,
        }
        ppool = "p" if split else "u"
        hidden_bytes = token_lengths[rid] * HIDDEN * DTYPE_BYTES
        push(item["scheduled_at_seconds"] + net(hidden_bytes), "ready", ppool, rid=rid, phase="prefill")

    while calendar:
        event = heapq.heappop(calendar)
        now = event.time
        if event.kind == "ready":
            heapq.heappush(queues[event.pool], (now, event.sequence, event.request_id, event.phase, event.decode_index))
            dispatch(event.pool, now)
        elif event.kind == "wake":
            dispatch(event.pool, now)
        else:
            idle[event.pool].append(event.worker)
            rid = event.request_id
            hidden_down = net((token_lengths[rid] if event.phase == "prefill" else 1) * HIDDEN * DTYPE_BYTES)
            if event.phase == "prefill":
                token_at = now + hidden_down
                state[rid]["token_times"].append(token_at)
                migration = 0.0
                if split:
                    kv_bytes = 2 * LAYERS * token_lengths[rid] * KV_HEADS * HEAD_DIM * DTYPE_BYTES
                    migration = 0.0001 + kv_bytes * 8.0 / (cloud_bandwidth_gbps * 1e9)
                    kv_bytes_total += kv_bytes
                    kv_transfer_seconds += migration
                dpool = "d" if split else "u"
                ready = now + migration + hidden_down + net(HIDDEN * DTYPE_BYTES)
                push(ready, "ready", dpool, rid=rid, phase="decode", decode_index=1)
            else:
                index = event.decode_index
                if index < state[rid]["max_tokens"]:
                    state[rid]["token_times"].append(now + hidden_down)
                    ready = now + hidden_down + net(HIDDEN * DTYPE_BYTES)
                    push(ready, "ready", event.pool, rid=rid, phase="decode", decode_index=index + 1)
                else:
                    state[rid]["done"] = now + hidden_down
            dispatch(event.pool, now)

    arrivals = [x["arrival"] for x in state.values()]
    finishes = [x["done"] for x in state.values()]
    wall = max(finishes) - min(arrivals)
    ttft = [x["token_times"][0] - x["arrival"] for x in state.values()]
    latencies = [x["done"] - x["arrival"] for x in state.values()]
    tpots = [b - a for x in state.values() for a, b in zip(x["token_times"], x["token_times"][1:])]
    pool_cards = pool_cards or dict(pools)
    card_count = sum(pool_cards.values())
    return {
        "name": name, "workers": pools, "wall_seconds": wall,
        "throughput_requests_per_second": len(events) / wall,
        "ttft_p95_seconds": percentile(ttft, .95), "tpot_p95_seconds": percentile(tpots, .95),
        "latency_p95_seconds": percentile(latencies, .95),
        "prefill_queue_wait_mean_seconds": sum(x["prefill_queue_wait"] for x in state.values()) / len(state),
        "decode_queue_wait_mean_seconds": sum(x["decode_queue_wait"] for x in state.values()) / len(state),
        "pool_busy_ratio": {pool: busy[pool] * pool_cards[pool] / pools[pool] / (wall * pool_cards[pool])
                            for pool in pools},
        "cloud_card_seconds": card_count * wall, "kv_transfer_gib": kv_bytes_total / 2**30,
        "kv_transfer_serialized_seconds": kv_transfer_seconds,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--schedule", type=Path, required=True)
    parser.add_argument("--serial-result", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cloud-bandwidth-gbps", type=float, default=100.0)
    args = parser.parse_args()
    schedule = json.loads(args.schedule.read_text(encoding="utf-8"))
    events = schedule["events"]
    measured = json.loads(args.serial_result.read_text(encoding="utf-8"))["cloud"]
    tokenizer = Tokenizer.from_file(str(args.tokenizer))
    token_lengths = {}
    weights = {}
    for item in events:
        formatted = f"<|im_start|>user\n{item['prompt']}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        n = len(tokenizer.encode(formatted, add_special_tokens=False).ids)
        token_lengths[item["id"]] = n
        weights[item["id"]] = n + 0.0008 * n * n
    scale = measured["prefill_busy_seconds"] / sum(weights.values())
    prefill_services = {rid: weight * scale for rid, weight in weights.items()}
    decode_service = measured["decode_busy_seconds"] / measured["decode_jobs"]
    configs = [
        ("1-unified", 0, 0, 1), ("2-unified", 0, 0, 2),
        ("1P-1D", 1, 1, 0), ("2P-1D", 2, 1, 0),
    ]
    results = [simulate(name, events, token_lengths, prefill_services, decode_service, p, d, u,
                        2.5, 10.0, args.cloud_bandwidth_gbps) for name, p, d, u in configs]
    base_cost = results[0]["cloud_card_seconds"]
    for result in results:
        result["relative_cost"] = result["cloud_card_seconds"] / base_cost
    output = {
        "method": "discrete-event simulation calibrated by real MiniMind sustained run",
        "calibration": {
            "prefill_busy_seconds": measured["prefill_busy_seconds"],
            "decode_busy_seconds": measured["decode_busy_seconds"],
            "decode_jobs": measured["decode_jobs"], "requests": len(events),
            "link": "500 km / 10 Gbps", "cloud_kv_link_gbps": args.cloud_bandwidth_gbps,
        },
        "results": results,
    }
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
