from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from capacity_model import Hardware, Model, find_max_qps


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--hardware", type=Path, required=True)
    parser.add_argument("--workload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    model = Model.load(args.model)
    base_hw = Hardware.load(args.hardware)
    base_workload = json.loads(args.workload.read_text(encoding="utf-8"))
    scenarios = []
    for compute_efficiency in (0.30, 0.42, 0.55):
        for bandwidth_efficiency in (0.50, 0.65, 0.75):
            hw = copy.copy(base_hw)
            hw.compute_efficiency = compute_efficiency
            hw.bandwidth_efficiency = bandwidth_efficiency
            best = None
            for prefill_batch in (1, 2, 4):
                for decode_batch in (8, 16, 32):
                    workload = copy.deepcopy(base_workload)
                    workload["prefill_batch"] = prefill_batch
                    workload["decode_batch"] = decode_batch
                    # Sensitivity scanning evaluates many configurations. Use a
                    # smaller deterministic simulation, then run the selected
                    # central point with the full profile in estimate.py.
                    if workload.get("queue_model") == "discrete_event":
                        workload["simulation_requests"] = 24
                        workload["simulation_warmup_requests"] = 4
                        workload["qps_search_iterations"] = 6
                    estimate = find_max_qps(model, hw, workload)
                    candidate = {
                        "compute_efficiency": compute_efficiency,
                        "bandwidth_efficiency": bandwidth_efficiency,
                        "prefill_batch": prefill_batch,
                        "decode_batch": decode_batch,
                        **estimate,
                    }
                    if best is None or candidate["qps"] > best["qps"]:
                        best = candidate
            scenarios.append(best)
    central = next(item for item in scenarios if item["compute_efficiency"] == 0.42 and item["bandwidth_efficiency"] == 0.65)
    conservative = next(item for item in scenarios if item["compute_efficiency"] == 0.30 and item["bandwidth_efficiency"] == 0.50)
    optimistic = next(item for item in scenarios if item["compute_efficiency"] == 0.55 and item["bandwidth_efficiency"] == 0.75)

    def full_refine(candidate):
        hw = copy.copy(base_hw)
        hw.compute_efficiency = candidate["compute_efficiency"]
        hw.bandwidth_efficiency = candidate["bandwidth_efficiency"]
        workload = copy.deepcopy(base_workload)
        workload["prefill_batch"] = candidate["prefill_batch"]
        workload["decode_batch"] = candidate["decode_batch"]
        estimate = find_max_qps(model, hw, workload)
        return {
            "compute_efficiency": candidate["compute_efficiency"],
            "bandwidth_efficiency": candidate["bandwidth_efficiency"],
            "prefill_batch": candidate["prefill_batch"],
            "decode_batch": candidate["decode_batch"],
            **estimate,
        }

    conservative_full = full_refine(conservative)
    central_full = full_refine(central)
    optimistic_full = full_refine(optimistic)
    result = {
        "central_best": central_full,
        "named_scenarios": {
            "conservative": conservative_full,
            "central": central_full,
            "optimistic": optimistic_full,
        },
        "sensitivity_qps": {
            "minimum": conservative_full["qps"],
            "maximum": optimistic_full["qps"],
        },
        "coarse_screening_scenarios": scenarios,
        "scenarios": scenarios,
        "warning": "The range reflects assumed kernel efficiencies, not statistical confidence intervals.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "scenarios"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
