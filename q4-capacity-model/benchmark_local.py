from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import torch


def timed(callable_, repeats: int) -> float:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        callable_()
        samples.append(time.perf_counter() - started)
    return sorted(samples)[len(samples) // 2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--q2-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    sys.path.insert(0, str(args.q2_dir.resolve()))
    from model import CloudModel, SafeTensorReader

    torch.manual_seed(0)
    model = CloudModel(SafeTensorReader(args.weights))
    observations = []
    cases = [(1, 8), (2, 8), (4, 8), (8, 8), (1, 32), (2, 32), (4, 32), (1, 64), (2, 64)]
    for batch, length in cases:
        hidden = [torch.randn(1, length, 768) for _ in range(batch)]

        def prefill():
            requests = [(str(uuid.uuid4()), item, True) for item in hidden]
            outputs = model.forward_batch(requests)
            for request_id, _, _ in requests:
                model.release(request_id)
            return outputs

        # Warm up kernels before collecting the median.
        prefill()
        prefill_s = timed(prefill, args.repeats)

        request_ids = [str(uuid.uuid4()) for _ in range(batch)]
        model.forward_batch([(rid, item, True) for rid, item in zip(request_ids, hidden)])
        one_token = [torch.randn(1, 1, 768) for _ in range(batch)]

        def decode():
            return model.forward_batch([(rid, item, False) for rid, item in zip(request_ids, one_token)])

        decode_s = timed(decode, args.repeats)
        for rid in request_ids:
            model.release(rid)
        observations.extend([
            {"phase": "prefill", "batch": batch, "input_tokens": length, "context_tokens": length, "seconds": prefill_s},
            {"phase": "decode", "batch": batch, "input_tokens": 1, "context_tokens": length, "seconds": decode_s},
        ])
        print(f"batch={batch:2d} length={length:3d}: prefill={prefill_s*1000:.2f}ms decode={decode_s*1000:.2f}ms")

    output = {
        "environment": {
            "torch_version": torch.__version__,
            "device": "cpu",
            "threads": torch.get_num_threads(),
            "weights": str(args.weights),
        },
        "observations": observations,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
