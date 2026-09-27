from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for path in args.input:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
        rows.append({
            "requested_qps": doc["configuration"]["offered_qps"],
            **doc["measured"],
            "cloud_utilization": doc["cloud"]["busy_ratio"],
            "cloud_queue_wait_ms": doc["cloud"]["average_queue_wait_ms"],
        })
    rows.sort(key=lambda row: row["requested_qps"])
    passing = [row for row in rows if row["capacity_pass"]]
    failing = [row for row in rows if not row["capacity_pass"]]
    result = {
        "definition": "highest sustainable offered QPS satisfying P95 TTFT and P95 TPOT SLO",
        "highest_passing_qps": max((row["actual_offered_qps"] for row in passing), default=None),
        "first_failing_qps": min((row["actual_offered_qps"] for row in failing), default=None),
        "capacity_is_bracketed": bool(passing and failing),
        "rows": rows,
        "note": "Arrival times follow the conditional Poisson construction with a fixed finite-window request count; actual_offered_qps is the measured comparison coordinate.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
