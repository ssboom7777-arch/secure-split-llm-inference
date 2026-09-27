from __future__ import annotations

import argparse
import json
import math
import re
import time
from pathlib import Path

import torch
from tokenizers import Tokenizer

from run_experiments import MODEL_DIR, ROOT, ShardedReader, configure
import model as qmodel


def extract_answer(text: str) -> str | None:
    marked = re.findall(r"####\s*(-?\d[\d,]*(?:\.\d+)?)", text)
    candidates = marked or re.findall(r"-?\d[\d,]*(?:\.\d+)?", text)
    if not candidates:
        return None
    try:
        number = float(candidates[-1].replace(",", ""))
        return str(int(number)) if number.is_integer() else str(number)
    except ValueError:
        return None


def prompt_ids(tokenizer: Tokenizer, question: str) -> list[int]:
    system = "Solve the math problem with concise reasoning. End with exactly '#### <number>'."
    chat = (f"<|im_start|>system\n{system}<|im_end|>\n"
            f"<|im_start|>user\n{question}\n/no_think<|im_end|>\n"
            f"<|im_start|>assistant\n")
    return tokenizer.encode(chat, add_special_tokens=False).ids


@torch.inference_mode()
def generate(enterprise, cloud, ids: list[int], max_new: int, request_id: str,
             ratio: float, rms: float, seed: int) -> list[int]:
    rng = torch.Generator().manual_seed(seed)

    def boundary(token_ids: list[int]) -> torch.Tensor:
        hidden = enterprise.embed(torch.tensor([token_ids], dtype=torch.long))
        if ratio:
            hidden = hidden + torch.randn(hidden.shape, generator=rng, dtype=hidden.dtype) * (ratio * rms)
        return hidden

    hidden = cloud.forward_request(request_id, boundary(ids), True)
    output: list[int] = []
    for _ in range(max_new):
        token = int(torch.argmax(enterprise.logits(hidden[:, -1, :]), dim=-1).item())
        if token in (151643, 151645):
            break
        output.append(token)
        hidden = cloud.forward_request(request_id, boundary([token]), False)
    cloud.release(request_id)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--max-new-tokens", type=int, default=64)
    parser.add_argument("--noise", type=float, nargs="+", default=[0.0, 0.5, 1.0])
    parser.add_argument("--output", type=Path, default=Path("results/qwen3-1.7b-gsm8k-split-noise.json"))
    args = parser.parse_args()
    dataset = ROOT / "q2-split-inference" / "data" / "gsm8k-test.jsonl"
    samples = [json.loads(x) for x in dataset.read_text(encoding="utf-8").splitlines()][:args.samples]

    torch.set_num_threads(min(8, torch.get_num_threads()))
    configure()
    old_dtype = torch.get_default_dtype(); torch.set_default_dtype(torch.bfloat16)
    reader = ShardedReader(MODEL_DIR)
    enterprise, cloud = qmodel.EnterpriseModel(reader), qmodel.CloudModel(reader)
    torch.set_default_dtype(old_dtype)
    tokenizer = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
    table = enterprise.embedding.weight
    rms = float(torch.median(torch.linalg.vector_norm(table.float(), dim=1)) / math.sqrt(table.shape[1]))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = {"status": "running", "model": "Qwen3-1.7B BF16 split inference",
              "dataset": "OpenAI GSM8K official test, first 50", "sample_count": len(samples),
              "max_new_tokens": args.max_new_tokens, "decoding": "greedy /no_think",
              "noise_application": "Gaussian noise at every transmitted prefill/decode embedding",
              "noise_sigma_over_embedding_rms": args.noise, "runs": []}
    total_started = time.perf_counter()
    for ratio in args.noise:
        run_started = time.perf_counter(); details = []
        for index, sample in enumerate(samples):
            started = time.perf_counter()
            ids = generate(enterprise, cloud, prompt_ids(tokenizer, sample["question"]),
                           args.max_new_tokens, f"gsm-{ratio}-{index}", ratio, rms, 20260927 + index)
            text = tokenizer.decode(ids, skip_special_tokens=True)
            gold = extract_answer(sample["answer"].split("####")[-1])
            predicted = extract_answer(text)
            details.append({"index": index, "question": sample["question"], "gold_answer": gold,
                            "predicted_answer": predicted, "correct": predicted == gold,
                            "response": text, "generated_tokens": len(ids),
                            "latency_seconds": time.perf_counter() - started})
            correct = sum(x["correct"] for x in details)
            print(f"[noise={ratio:g} {index+1}/{len(samples)}] correct={correct}", flush=True)
            partial = {**result, "runs": result["runs"] + [{"noise_ratio": ratio,
                       "status": "running", "completed": len(details), "details": details}]}
            args.output.write_text(json.dumps(partial, ensure_ascii=False, indent=2), encoding="utf-8")
        elapsed = time.perf_counter() - run_started
        result["runs"].append({"noise_ratio": ratio, "correct_count": sum(x["correct"] for x in details),
                               "accuracy": sum(x["correct"] for x in details)/len(details),
                               "mean_generated_tokens": sum(x["generated_tokens"] for x in details)/len(details),
                               "elapsed_seconds": elapsed, "details": details})
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["status"] = "complete"; result["elapsed_seconds"] = time.perf_counter()-total_started
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps([{k:v for k,v in run.items() if k != "details"} for run in result["runs"]],
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
