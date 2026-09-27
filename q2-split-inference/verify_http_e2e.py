from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

import torch
from tokenizers import Tokenizer

from enterprise_server import format_prompt
from model import MonolithicModel, SafeTensorReader


PROMPTS = [
    "Hello, introduce yourself briefly.",
    "What is two plus three?",
    "What is the capital of China?",
    "中国的首都是哪里？",
    "请用一句话说明为什么要保护企业数据。",
]


def post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read().decode("utf-8"))


def generate_monolithic(model: MonolithicModel, tokenizer: Tokenizer, prompt: str, steps: int, request_id: str):
    input_ids = tokenizer.encode(format_prompt(prompt), add_special_tokens=False).ids
    generated: list[int] = []
    with torch.inference_mode():
        tokens = torch.tensor([input_ids], dtype=torch.long)
        logits = model.step(request_id, tokens, reset=True)
        for _ in range(steps):
            token = int(torch.argmax(logits, dim=-1).item())
            if token == 2:
                break
            generated.append(token)
            logits = model.step(
                request_id, torch.tensor([[token]], dtype=torch.long), reset=False
            )
    model.cloud.release(request_id)
    return generated


def main():
    parser = argparse.ArgumentParser(description="Compare monolithic inference with the full HTTP split path")
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8100/generate")
    parser.add_argument("--steps", type=int, default=16)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    tokenizer = Tokenizer.from_file(str(args.tokenizer))
    baseline = MonolithicModel(SafeTensorReader(args.weights))
    details = []
    started = time.perf_counter()
    for index, prompt in enumerate(PROMPTS):
        baseline_ids = generate_monolithic(baseline, tokenizer, prompt, args.steps, f"baseline-{index}")
        response = post_json(args.url, {
            "prompt": prompt, "max_new_tokens": args.steps, "stream": False
        })
        if "error" in response:
            raise RuntimeError(response["error"])
        http_ids = response["token_ids"]
        details.append({
            "prompt": prompt,
            "baseline_token_ids": baseline_ids,
            "http_split_token_ids": http_ids,
            "token_ids_identical": baseline_ids == http_ids,
            "baseline_text": tokenizer.decode(baseline_ids, skip_special_tokens=True),
            "http_split_text": response["text"],
            "text_identical": tokenizer.decode(baseline_ids, skip_special_tokens=True) == response["text"],
        })
        print(f"[{index+1}/{len(PROMPTS)}] tokens_equal={baseline_ids == http_ids} prompt={prompt}", flush=True)
    summary = {
        "prompt_count": len(details),
        "max_new_tokens": args.steps,
        "token_sequence_match_rate": sum(x["token_ids_identical"] for x in details) / len(details),
        "text_match_rate": sum(x["text_identical"] for x in details) / len(details),
        "path_under_test": "client -> enterprise HTTP -> Base64 hidden -> cloud HTTP -> enterprise LM head",
        "elapsed_seconds": time.perf_counter() - started,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"summary": summary, "cases": details}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["token_sequence_match_rate"] != 1.0:
        raise SystemExit("HTTP split output diverged from monolithic baseline")


if __name__ == "__main__":
    main()
