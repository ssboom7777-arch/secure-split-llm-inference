from __future__ import annotations

import json
import heapq
import math
import random
import statistics
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Model:
    name: str
    hidden_size: int
    intermediate_size: int
    layers: int
    attention_heads: int
    kv_heads: int
    head_dim: int
    vocab_size: int
    dtype_bytes: int
    parameter_count: float

    @property
    def kv_width(self) -> int:
        return self.kv_heads * self.head_dim

    @classmethod
    def load(cls, path: Path):
        return cls(**json.loads(path.read_text(encoding="utf-8")))


@dataclass
class Hardware:
    name: str
    devices: int
    peak_tflops: float
    memory_bandwidth_gbps: float
    memory_gb: float
    compute_efficiency: float
    bandwidth_efficiency: float
    tensor_parallel_efficiency: float

    @classmethod
    def load(cls, path: Path):
        return cls(**json.loads(path.read_text(encoding="utf-8")))


def transformer_linear_flops(model: Model, tokens: int) -> float:
    h, kv, i = model.hidden_size, model.kv_width, model.intermediate_size
    per_layer_per_token = 2.0 * (2 * h * h + 2 * h * kv + 3 * h * i)
    return model.layers * tokens * per_layer_per_token


def attention_flops(model: Model, query_tokens: int, context_tokens: int) -> float:
    # QK^T and AV: two matmuls, each multiply-add counts as two FLOPs.
    return 4.0 * model.layers * model.hidden_size * query_tokens * context_tokens


def prefill_flops(model: Model, batch: int, input_tokens: int) -> float:
    return batch * (
        transformer_linear_flops(model, input_tokens)
        + attention_flops(model, input_tokens, input_tokens)
    )


def decode_flops(model: Model, batch: int, context_tokens: int) -> float:
    return batch * (
        transformer_linear_flops(model, 1)
        + attention_flops(model, 1, context_tokens)
    )


def model_bytes(model: Model) -> float:
    return model.parameter_count * model.dtype_bytes


def kv_bytes_per_request(model: Model, context_tokens: int) -> float:
    return 2.0 * model.layers * model.kv_width * context_tokens * model.dtype_bytes


def effective_compute(hw: Hardware) -> float:
    return hw.devices * hw.peak_tflops * 1e12 * hw.compute_efficiency * hw.tensor_parallel_efficiency


def effective_bandwidth(hw: Hardware) -> float:
    return hw.devices * hw.memory_bandwidth_gbps * 1e9 * hw.bandwidth_efficiency * hw.tensor_parallel_efficiency


def prefill_time(model: Model, hw: Hardware, batch: int, input_tokens: int) -> float:
    compute = prefill_flops(model, batch, input_tokens) / effective_compute(hw)
    # One approximate model-weight stream per physical batch.
    memory = model_bytes(model) / effective_bandwidth(hw)
    return max(compute, memory)


def decode_time(model: Model, hw: Hardware, batch: int, context_tokens: int) -> float:
    compute = decode_flops(model, batch, context_tokens) / effective_compute(hw)
    memory_bytes = model_bytes(model) + batch * kv_bytes_per_request(model, context_tokens)
    memory = memory_bytes / effective_bandwidth(hw)
    return max(compute, memory)


def network_one_way_seconds(model: Model, tokens: int, distance_km: float, bandwidth_gbps: float) -> float:
    propagation = distance_km * 5e-6
    activation_bytes = tokens * model.hidden_size * model.dtype_bytes
    serialization = activation_bytes * 8 / (bandwidth_gbps * 1e9)
    return propagation + serialization


def predict(model: Model, hw: Hardware, workload: dict, qps: float) -> dict:
    inp, out = workload["input_tokens"], workload["output_tokens"]
    configured_pb, configured_db = workload["prefill_batch"], workload["decode_batch"]
    # A configured batch is only a ceiling. At a given arrival rate there may not
    # be enough live requests to fill it, especially for decode.
    live_requests = max(1.0, qps * workload["request_duration_seconds"])
    db = min(float(configured_db), live_requests)
    arrivals_in_window = max(1.0, qps * workload["batch_window_ms"] / 1000.0)
    pb = min(float(configured_pb), arrivals_in_window)
    prefill_batch_s = prefill_time(model, hw, pb, inp)
    decode_step_s = decode_time(model, hw, db, inp + out / 2)
    prefill_service = prefill_batch_s / pb
    decode_service = out * decode_step_s / db
    service_per_request = prefill_service + decode_service
    utilization = qps * service_per_request

    # M/M/1 is deliberately conservative near saturation and makes the SLO cliff explicit.
    if utilization >= 1:
        queue_s = math.inf
    else:
        queue_s = service_per_request * utilization / max(1.0 - utilization, 1e-9)
    prefill_network = 2 * network_one_way_seconds(
        model, inp, workload["distance_km"], workload["bandwidth_gbps"]
    )
    decode_network = 2 * network_one_way_seconds(
        model, 1, workload["distance_km"], workload["bandwidth_gbps"]
    )
    batch_wait = workload["batch_window_ms"] / 2000.0
    ttft = workload["enterprise_prefill_ms"] / 1000 + prefill_network + batch_wait + prefill_batch_s + queue_s
    tpot = workload["enterprise_decode_ms"] / 1000 + decode_network + batch_wait + decode_step_s + queue_s / max(out, 1)
    kv_gb = qps * workload["request_duration_seconds"] * kv_bytes_per_request(model, inp + out) / 1e9
    weights_gb = model_bytes(model) / 1e9
    memory_ok = weights_gb + kv_gb <= hw.devices * hw.memory_gb * 0.90
    return {
        "qps": qps,
        "ttft_seconds": ttft,
        "tpot_seconds": tpot,
        "utilization": utilization,
        "effective_prefill_batch": pb,
        "effective_decode_batch": db,
        "queue_seconds": queue_s,
        "prefill_batch_seconds": prefill_batch_s,
        "decode_batch_step_seconds": decode_step_s,
        "network_prefill_roundtrip_seconds": prefill_network,
        "network_decode_roundtrip_seconds": decode_network,
        "weights_gb": weights_gb,
        "estimated_kv_cache_gb": kv_gb,
        "memory_ok": memory_ok,
    }


def find_max_qps(model: Model, hw: Hardware, workload: dict) -> dict:
    if workload.get("queue_model") == "discrete_event":
        return find_max_qps_discrete(model, hw, workload)
    slo = workload["slo"]

    def passes(qps: float) -> bool:
        result = predict(model, hw, workload, qps)
        return (
            result["memory_ok"]
            and result["utilization"] < workload["max_utilization"]
            and result["ttft_seconds"] <= slo["ttft_seconds"]
            and result["tpot_seconds"] <= slo["tpot_seconds"]
        )

    low, high = 0.0, 1.0
    while passes(high) and high < 1e6:
        low, high = high, high * 2
    for _ in range(80):
        middle = (low + high) / 2
        if passes(middle):
            low = middle
        else:
            high = middle
    result = predict(model, hw, workload, low)
    probes = {
        "resource_limit": result["utilization"] / workload["max_utilization"],
        "ttft_limit": result["ttft_seconds"] / slo["ttft_seconds"],
        "tpot_limit": result["tpot_seconds"] / slo["tpot_seconds"],
        "memory_limit": (result["weights_gb"] + result["estimated_kv_cache_gb"]) / (hw.devices * hw.memory_gb * .9),
    }
    result["limiting_constraint"] = max(probes, key=probes.get)
    result["constraint_ratios"] = probes
    return result


def _percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(math.ceil(p * len(ordered)) - 1, len(ordered) - 1)]


def simulate_discrete(model: Model, hw: Hardware, workload: dict, qps: float) -> dict:
    """Token-level split-inference simulation with separate Prefill/Decode queues.

    It intentionally models one logical cloud execution pool. Dynamic batches are
    formed from jobs already ready at dispatch time; the optional batch window is
    used only when the cloud would otherwise be idle.
    """
    inp, out = int(workload["input_tokens"]), int(round(workload["output_tokens"]))
    count = int(workload.get("simulation_requests", 400))
    warmup = min(int(workload.get("simulation_warmup_requests", 50)), max(0, count // 4))
    seed = int(workload.get("simulation_seed", 20260927))
    rng = random.Random(seed)
    arrivals, at = [], 0.0
    for _ in range(count):
        at += rng.expovariate(max(qps, 1e-12))
        arrivals.append(at)

    prefill_boundary = workload.get("enterprise_prefill_boundary_ms", workload.get("enterprise_prefill_ms", 0.0)) / 1000
    decode_boundary = workload.get("enterprise_decode_boundary_ms", workload.get("enterprise_decode_ms", 0.0)) / 1000
    prefill_in, prefill_out = prefill_boundary / 2, prefill_boundary / 2
    decode_in, decode_out = decode_boundary / 2, decode_boundary / 2
    prefill_up = network_one_way_seconds(model, inp, workload["distance_km"], workload["bandwidth_gbps"])
    prefill_down = prefill_up
    decode_up = network_one_way_seconds(model, 1, workload["distance_km"], workload["bandwidth_gbps"])
    decode_down = decode_up
    multiplier = float(workload.get("cloud_service_time_multiplier", 1.0))
    fixed_s = float(workload.get("cloud_batch_fixed_overhead_ms", 0.0)) / 1000
    batch_window = float(workload.get("batch_window_ms", 0.0)) / 1000
    pb_cap, db_cap = int(workload["prefill_batch"]), int(workload["decode_batch"])
    max_decode_burst = int(workload.get("max_decode_burst", 4))
    prefill_starvation_s = float(workload.get("prefill_starvation_ms", 500.0)) / 1000

    # future item: ready time, stable sequence, phase, request id, produced-token index
    future, sequence = [], 0
    for request_id, arrival in enumerate(arrivals):
        heapq.heappush(future, (arrival + prefill_in + prefill_up, sequence, "prefill", request_id, 0))
        sequence += 1
    ready_p, ready_d = [], []
    first_tokens = [None] * count
    token_times: list[list[float]] = [[] for _ in range(count)]
    now, cloud_busy, decode_burst = 0.0, 0.0, 0

    def admit(until: float) -> None:
        while future and future[0][0] <= until:
            item = heapq.heappop(future)
            (ready_d if item[2] == "decode" else ready_p).append(item)

    while future or ready_p or ready_d:
        admit(now)
        if not ready_p and not ready_d:
            now = future[0][0]
            admit(now)
        if batch_window > 0 and future and not ready_d:
            now += batch_window
            admit(now)
        oldest_prefill_wait = now - ready_p[0][0] if ready_p else 0.0
        force_prefill = bool(ready_p) and (
            not ready_d or decode_burst >= max_decode_burst or oldest_prefill_wait >= prefill_starvation_s
        )
        phase = "prefill" if force_prefill else "decode"
        decode_burst = decode_burst + 1 if phase == "decode" else 0
        queue = ready_d if phase == "decode" else ready_p
        cap = db_cap if phase == "decode" else pb_cap
        batch = [queue.pop(0) for _ in range(min(cap, len(queue)))]
        size = len(batch)
        if phase == "decode":
            context = inp + max(item[4] for item in batch)
            service = decode_time(model, hw, size, context) * multiplier + fixed_s
        else:
            service = prefill_time(model, hw, size, inp) * multiplier + fixed_s
        now += service
        cloud_busy += service
        for _, _, item_phase, request_id, produced in batch:
            if item_phase == "prefill":
                token_at = now + prefill_down + prefill_out
                first_tokens[request_id] = token_at
                token_times[request_id].append(token_at)
                if out > 1:
                    ready_at = token_at + decode_in + decode_up
                    heapq.heappush(future, (ready_at, sequence, "decode", request_id, 1))
                    sequence += 1
            else:
                token_at = now + decode_down + decode_out
                token_times[request_id].append(token_at)
                if produced + 1 < out:
                    ready_at = token_at + decode_in + decode_up
                    heapq.heappush(future, (ready_at, sequence, "decode", request_id, produced + 1))
                    sequence += 1

    selected = range(warmup, count)
    ttfts = [first_tokens[i] - arrivals[i] for i in selected]
    per_request_tpot = [statistics.mean(b - a for a, b in zip(token_times[i], token_times[i][1:])) for i in selected if len(token_times[i]) > 1]
    durations = [token_times[i][-1] - arrivals[i] for i in selected]
    p = float(workload.get("latency_percentile", 0.95))
    observation_start = arrivals[warmup] if warmup < count else arrivals[0]
    observation_span = max(now - observation_start, 1e-9)
    utilization = min(1.0, cloud_busy / max(now, 1e-9))
    avg_duration = statistics.mean(durations)
    kv_gb = qps * avg_duration * kv_bytes_per_request(model, inp + out) / 1e9
    weights_gb = model_bytes(model) / 1e9
    return {
        "qps": qps,
        "ttft_seconds": _percentile(ttfts, p),
        "tpot_seconds": _percentile(per_request_tpot, p),
        "utilization": utilization,
        "simulated_completed_qps": (count - warmup) / observation_span,
        "mean_request_duration_seconds": avg_duration,
        "weights_gb": weights_gb,
        "estimated_kv_cache_gb": kv_gb,
        "memory_ok": weights_gb + kv_gb <= hw.devices * hw.memory_gb * 0.90,
        "simulation_requests": count,
        "simulation_warmup_requests": warmup,
        "latency_percentile": p,
        "queue_model": "token-level discrete event, decode-first",
    }


def find_max_qps_discrete(model: Model, hw: Hardware, workload: dict) -> dict:
    slo = workload["slo"]

    def passes(result: dict) -> bool:
        return (
            result["memory_ok"]
            and result["utilization"] <= workload["max_utilization"]
            and result["ttft_seconds"] <= slo["ttft_seconds"]
            and result["tpot_seconds"] <= slo["tpot_seconds"]
        )

    low, high = 0.001, 1.0
    low_result = simulate_discrete(model, hw, workload, low)
    while passes(simulate_discrete(model, hw, workload, high)) and high < 1e4:
        low, high = high, high * 2
    for _ in range(int(workload.get("qps_search_iterations", 18))):
        middle = (low + high) / 2
        result = simulate_discrete(model, hw, workload, middle)
        if passes(result):
            low, low_result = middle, result
        else:
            high = middle
    result = simulate_discrete(model, hw, workload, low)
    ratios = {
        "resource_limit": result["utilization"] / workload["max_utilization"],
        "ttft_limit": result["ttft_seconds"] / slo["ttft_seconds"],
        "tpot_limit": result["tpot_seconds"] / slo["tpot_seconds"],
        "memory_limit": (result["weights_gb"] + result["estimated_kv_cache_gb"]) / (hw.devices * hw.memory_gb * .9),
    }
    result["limiting_constraint"] = max(ratios, key=ratios.get)
    result["constraint_ratios"] = ratios
    result["first_failing_qps"] = high
    return result
