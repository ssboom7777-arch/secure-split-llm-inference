from __future__ import annotations

import argparse
import json
from pathlib import Path

from capacity_model import Hardware, Model, find_max_qps


def main():
    parser = argparse.ArgumentParser(description="Estimate split-inference capacity under latency SLOs")
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--hardware", type=Path, required=True)
    parser.add_argument("--workload", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    model, hardware = Model.load(args.model), Hardware.load(args.hardware)
    workload = json.loads(args.workload.read_text(encoding="utf-8"))
    result = {
        "model": model.name,
        "hardware": hardware.name,
        "workload": workload,
        "prediction": find_max_qps(model, hardware, workload),
        "classification": (
            "token-level discrete-event prediction, not a measurement on the target GPU"
            if workload.get("queue_model") == "discrete_event"
            else "analytical prediction, not a measurement on the target GPU"
        ),
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
