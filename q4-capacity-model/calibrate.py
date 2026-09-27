from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from capacity_model import Model, decode_flops, kv_bytes_per_request, model_bytes, prefill_flops


def predicted(obs: dict, model: Model, compute_flops_s: float, bandwidth_bytes_s: float) -> float:
    batch = obs["batch"]
    if obs["phase"] == "prefill":
        flops = prefill_flops(model, batch, obs["input_tokens"])
        memory = model_bytes(model)
    else:
        flops = decode_flops(model, batch, obs["context_tokens"])
        memory = model_bytes(model) + batch * kv_bytes_per_request(model, obs["context_tokens"])
    return max(flops / compute_flops_s, memory / bandwidth_bytes_s)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--q3-result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    model = Model.load(args.model)
    benchmark = json.loads(args.benchmark.read_text(encoding="utf-8"))
    observations = benchmark["observations"]
    # Deterministic holdout: the two longest-context cases are never used for fitting.
    train = [o for o in observations if o["context_tokens"] < 64]
    holdout = [o for o in observations if o["context_tokens"] == 64]
    best = None
    # Grid fit two effective, directly interpretable Roofline rates.
    for c_exp in [x / 10 for x in range(8, 131)]:
        compute = 10 ** c_exp
        for b_exp in [x / 10 for x in range(8, 131)]:
            bandwidth = 10 ** b_exp
            errors = [abs(math.log(predicted(o, model, compute, bandwidth) / o["seconds"])) for o in train]
            score = sum(errors) / len(errors)
            if best is None or score < best[0]:
                best = (score, compute, bandwidth)
    _, compute, bandwidth = best

    def evaluate(rows):
        details = []
        for row in rows:
            estimate = predicted(row, model, compute, bandwidth)
            details.append({**row, "predicted_seconds": estimate, "absolute_percentage_error": abs(estimate / row["seconds"] - 1)})
        return {
            "mape": sum(item["absolute_percentage_error"] for item in details) / len(details),
            "details": details,
        }

    q3 = json.loads(args.q3_result.read_text(encoding="utf-8"))
    result = {
        "fitted_effective_compute_gflops": compute / 1e9,
        "fitted_effective_memory_bandwidth_gbps": bandwidth / 1e9,
        "training": evaluate(train),
        "holdout_validation": evaluate(holdout),
        "q3_cross_check": {
            "logical_jobs": q3["cloud"]["jobs_completed"],
            "physical_batches": q3["cloud"]["forward_batches"],
            "average_batch_size": q3["cloud"]["average_batch_size"],
            "measured_cloud_busy_seconds": q3["cloud"]["busy_seconds"],
            "purpose": "independent evidence that batching reduces physical forwards; prompt lengths differ from microbenchmark, so it is not used to fit rates",
        },
        "warning": "CPU-fitted rates validate the equations locally and are not transferred to H20; target prediction uses an explicit H20 efficiency profile.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("training", "holdout_validation")}, ensure_ascii=False, indent=2))
    print(f"train MAPE={result['training']['mape']:.1%}; holdout MAPE={result['holdout_validation']['mape']:.1%}")


if __name__ == "__main__":
    main()
