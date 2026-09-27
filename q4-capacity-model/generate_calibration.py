from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from capacity_model import (
    Hardware,
    Model,
    decode_time,
    network_one_way_seconds,
    prefill_time,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate local capacity profiles from Q2/Q3 measurements")
    parser.add_argument("--local-validation", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--open-loop", type=Path, action="append", required=True)
    parser.add_argument("--template-workload", type=Path, required=True)
    parser.add_argument("--hardware-output", type=Path, required=True)
    parser.add_argument("--workload-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    args = parser.parse_args()

    fitted = json.loads(args.local_validation.read_text(encoding="utf-8-sig"))
    compute_gflops = fitted["fitted_effective_compute_gflops"]
    bandwidth_gbps = fitted["fitted_effective_memory_bandwidth_gbps"]
    hardware_doc = {
        "name": "Local CPU generated from Q2/Q3 calibration",
        "devices": 1,
        "peak_tflops": compute_gflops / 1000,
        "memory_bandwidth_gbps": bandwidth_gbps,
        "memory_gb": 32.0,
        "compute_efficiency": 1.0,
        "bandwidth_efficiency": 1.0,
        "tensor_parallel_efficiency": 1.0,
    }
    hardware = Hardware(**hardware_doc)
    model = Model.load(args.model)
    workload = json.loads(args.template_workload.read_text(encoding="utf-8-sig"))
    inp = int(workload["input_tokens"])

    measured_busy = 0.0
    predicted_busy = 0.0
    docs = []
    for path in args.open_loop:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
        docs.append(doc)
        measured_busy += doc["cloud"]["busy_seconds"]
        for request in doc["requests"]:
            predicted_busy += prefill_time(model, hardware, 1, inp)
            for step in range(request["generated_steps"]):
                predicted_busy += decode_time(model, hardware, 1, inp + step)
    multiplier = measured_busy / predicted_busy

    decode_boundaries = []
    prefill_boundaries = []
    prefill_network = 2 * network_one_way_seconds(
        model, inp, workload["distance_km"], workload["bandwidth_gbps"]
    )
    decode_network = 2 * network_one_way_seconds(
        model, 1, workload["distance_km"], workload["bandwidth_gbps"]
    )
    for doc in docs:
        average_cloud_decode = statistics.mean(
            decode_time(model, hardware, 1, inp + step) * multiplier
            for request in doc["requests"]
            for step in range(request["generated_steps"])
        )
        tpots = [
            request["timing"]["mean_tpot_seconds"]
            for request in doc["requests"]
            if request["timing"]["mean_tpot_seconds"] > 0
        ]
        decode_boundaries.append(max(0.0, statistics.mean(tpots) - decode_network - average_cloud_decode))

        mean_ttft = statistics.mean(request["timing"]["ttft_seconds"] for request in doc["requests"])
        prefill_queue = doc["cloud"].get("prefill_average_queue_wait_ms", 0.0) / 1000
        cloud_prefill = prefill_time(model, hardware, 1, inp) * multiplier
        prefill_boundaries.append(max(0.0, mean_ttft - prefill_network - prefill_queue - cloud_prefill))

    workload["enterprise_prefill_boundary_ms"] = 1000 * statistics.mean(prefill_boundaries)
    workload["enterprise_decode_boundary_ms"] = 1000 * statistics.mean(decode_boundaries)
    workload["cloud_service_time_multiplier"] = multiplier
    workload["calibration_note"] = (
        "Generated from Q2 fitted Roofline rates and Q3 low-load open-loop measurements; "
        "P95 values are held out and are not folded into fixed service costs."
    )
    report = {
        "inputs": [str(path) for path in args.open_loop],
        "fitted_effective_compute_gflops": compute_gflops,
        "fitted_effective_memory_bandwidth_gbps": bandwidth_gbps,
        "predicted_cloud_busy_seconds_before_correction": predicted_busy,
        "measured_cloud_busy_seconds": measured_busy,
        "cloud_service_time_multiplier": multiplier,
        "enterprise_prefill_boundary_ms": workload["enterprise_prefill_boundary_ms"],
        "enterprise_decode_boundary_ms": workload["enterprise_decode_boundary_ms"],
        "method": "mean low-load costs calibrate fixed terms; higher-rate P95 points validate capacity",
    }
    for path, payload in ((args.hardware_output, hardware_doc), (args.workload_output, workload), (args.report_output, report)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
