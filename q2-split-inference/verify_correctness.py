from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

import torch
from tokenizers import Tokenizer

from enterprise_server import format_prompt
from model import CloudModel, EnterpriseModel, MonolithicModel, SafeTensorReader


def greedy_local(enterprise, cloud, tokenizer, prompt, steps, request_id):
    ids = tokenizer.encode(format_prompt(prompt), add_special_tokens=False).ids
    generated = []
    hidden = cloud.forward_request(request_id, enterprise.embed(torch.tensor([ids])), reset=True)
    for _ in range(steps):
        token = int(torch.argmax(enterprise.logits(hidden[:, -1, :]), dim=-1).item())
        generated.append(token)
        if token == 2:
            break
        hidden = cloud.forward_request(
            request_id, enterprise.embed(torch.tensor([[token]])), reset=False
        )
    cloud.release(request_id)
    return generated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--prompt", default="中国的首都是哪里？")
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--output", type=Path, default=Path("results/correctness.json"))
    args = parser.parse_args()
    tokenizer = Tokenizer.from_file(str(args.tokenizer))
    reader = SafeTensorReader(args.weights)
    enterprise = EnterpriseModel(reader)
    split_cloud = CloudModel(reader)
    split_ids = greedy_local(enterprise, split_cloud, tokenizer, args.prompt, args.steps, "split")

    baseline = MonolithicModel(reader)
    baseline_ids = greedy_local(
        baseline.enterprise, baseline.cloud, tokenizer, args.prompt, args.steps, "baseline"
    )
    result = {
        "prompt": args.prompt,
        "split_token_ids": split_ids,
        "baseline_token_ids": baseline_ids,
        "token_exact_match": split_ids == baseline_ids,
        "split_text": tokenizer.decode(split_ids, skip_special_tokens=True),
        "baseline_text": tokenizer.decode(baseline_ids, skip_special_tokens=True),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["token_exact_match"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
