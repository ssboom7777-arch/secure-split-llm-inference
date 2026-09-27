import argparse
import json
from pathlib import Path


parser = argparse.ArgumentParser()
parser.add_argument("serial", type=Path)
parser.add_argument("optimized", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
s = json.loads(args.serial.read_text(encoding="utf-8"))
o = json.loads(args.optimized.read_text(encoding="utf-8"))
sc, oc = s["client"], o["client"]
s_text = [x["text"] for x in s["requests"]]
o_text = [x["text"] for x in o["requests"]]
summary = {
    "same_schedule": s["schedule_assumptions"] == o["schedule_assumptions"],
    "outputs_identical": s_text == o_text,
    "request_count": sc["request_count"],
    "serial": {"client": sc, "cloud": s["cloud"]},
    "optimized": {"client": oc, "cloud": o["cloud"]},
    "changes": {
        "throughput_speedup": oc["generated_steps_per_second"] / sc["generated_steps_per_second"],
        "p95_ttft_ratio": oc["ttft_p95_seconds"] / sc["ttft_p95_seconds"],
        "p95_tpot_ratio": oc["tpot_p95_seconds"] / sc["tpot_p95_seconds"],
        "relative_cost_per_request": (
            oc["wall_seconds_including_drain"] / oc["request_count"]
        ) / (sc["wall_seconds_including_drain"] / sc["request_count"]),
    },
    "cost_note": "Same fixed arrival trace and work; cost ratio uses instance wall time per completed request.",
}
args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(summary["changes"], ensure_ascii=False, indent=2))
