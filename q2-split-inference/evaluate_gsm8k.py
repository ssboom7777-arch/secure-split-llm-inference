from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.request
from pathlib import Path

import torch
from tokenizers import Tokenizer

from enterprise_server import format_prompt
from model import CloudModel, EnterpriseModel, MonolithicModel, SafeTensorReader


DATASET_URL = "https://raw.githubusercontent.com/openai/grade-school-math/master/grade_school_math/data/test.jsonl"
DATASET_SHA256 = "3730d312f6e3440559ace48831e51066acaca737f6eabec99bccb9e4b3c39d14"


def ensure_dataset(path: Path) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading official GSM8K test set to {path} ...", flush=True)
        request = urllib.request.Request(DATASET_URL, headers={"User-Agent": "q2-gsm8k-poc/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response:
            path.write_bytes(response.read())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != DATASET_SHA256:
        raise RuntimeError(f"GSM8K checksum mismatch: {digest}")


def load_samples(path: Path, count: int, offset: int) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return rows[offset : offset + count]


def evaluation_prompt(question: str) -> str:
    return (
        "Solve this grade-school math problem. Show concise reasoning and end your answer "
        "with exactly '#### <number>'.\n\nQuestion: " + question
    )


def generate_split(
    enterprise: EnterpriseModel,
    cloud: CloudModel,
    tokenizer: Tokenizer,
    prompt: str,
    max_new_tokens: int,
    request_id: str,
) -> list[int]:
    input_ids = tokenizer.encode(format_prompt(prompt), add_special_tokens=False).ids
    generated: list[int] = []
    with torch.inference_mode():
        hidden = cloud.forward_request(
            request_id, enterprise.embed(torch.tensor([input_ids], dtype=torch.long)), reset=True
        )
        for _ in range(max_new_tokens):
            token = int(torch.argmax(enterprise.logits(hidden[:, -1, :]), dim=-1).item())
            if token == 2:
                break
            generated.append(token)
            hidden = cloud.forward_request(
                request_id,
                enterprise.embed(torch.tensor([[token]], dtype=torch.long)),
                reset=False,
            )
    cloud.release(request_id)
    return generated


def extract_answer(text: str) -> str | None:
    marked = re.findall(r"####\s*(-?\d[\d,]*(?:\.\d+)?)", text)
    candidates = marked or re.findall(r"-?\d[\d,]*(?:\.\d+)?", text)
    if not candidates:
        return None
    value = candidates[-1].replace(",", "")
    try:
        number = float(value)
        return str(int(number)) if number.is_integer() else str(number)
    except ValueError:
        return value


def main():
    parser = argparse.ArgumentParser(description="Compare monolithic and split MiniMind on GSM8K")
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=Path("data/gsm8k-test.jsonl"))
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--max-new-tokens", type=int, default=64)
    parser.add_argument("--output", type=Path, default=Path("results/gsm8k-comparison.json"))
    args = parser.parse_args()

    ensure_dataset(args.dataset)
    samples = load_samples(args.dataset, args.samples, args.offset)
    tokenizer = Tokenizer.from_file(str(args.tokenizer))
    reader = SafeTensorReader(args.weights)
    split_enterprise, split_cloud = EnterpriseModel(reader), CloudModel(reader)
    baseline = MonolithicModel(reader)

    details = []
    started = time.perf_counter()
    for index, sample in enumerate(samples):
        prompt = evaluation_prompt(sample["question"])
        baseline_ids = generate_split(
            baseline.enterprise, baseline.cloud, tokenizer, prompt, args.max_new_tokens, f"baseline-{index}"
        )
        split_ids = generate_split(
            split_enterprise, split_cloud, tokenizer, prompt, args.max_new_tokens, f"split-{index}"
        )
        baseline_text = tokenizer.decode(baseline_ids, skip_special_tokens=True)
        split_text = tokenizer.decode(split_ids, skip_special_tokens=True)
        gold = extract_answer(sample["answer"].split("####")[-1])
        baseline_answer, split_answer = extract_answer(baseline_text), extract_answer(split_text)
        details.append({
            "index": args.offset + index,
            "question": sample["question"],
            "gold_answer": gold,
            "baseline_answer": baseline_answer,
            "split_answer": split_answer,
            "baseline_correct": baseline_answer == gold,
            "split_correct": split_answer == gold,
            "token_ids_identical": baseline_ids == split_ids,
            "texts_identical": baseline_text == split_text,
            "baseline_text": baseline_text,
            "split_text": split_text,
            "generated_tokens": len(split_ids),
        })
        print(
            f"[{index + 1}/{len(samples)}] gold={gold} baseline={baseline_answer} "
            f"split={split_answer} tokens_equal={baseline_ids == split_ids}",
            flush=True,
        )

    total = len(details)
    summary = {
        "dataset": "OpenAI GSM8K official test split",
        "dataset_url": DATASET_URL,
        "dataset_sha256": DATASET_SHA256,
        "sample_offset": args.offset,
        "sample_count": total,
        "max_new_tokens": args.max_new_tokens,
        "decoding": "greedy",
        "baseline_exact_match": sum(x["baseline_correct"] for x in details) / total,
        "split_exact_match": sum(x["split_correct"] for x in details) / total,
        "token_sequence_match_rate": sum(x["token_ids_identical"] for x in details) / total,
        "text_match_rate": sum(x["texts_identical"] for x in details) / total,
        "accuracy_delta_split_minus_baseline": (
            sum(x["split_correct"] for x in details) - sum(x["baseline_correct"] for x in details)
        ) / total,
        "elapsed_seconds": time.perf_counter() - started,
    }
    result = {"summary": summary, "samples": details}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["token_sequence_match_rate"] != 1.0:
        raise SystemExit("Split output diverged from monolithic baseline")


if __name__ == "__main__":
    main()
