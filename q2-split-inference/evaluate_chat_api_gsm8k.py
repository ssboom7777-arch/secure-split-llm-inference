from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from pathlib import Path


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
        return None


def complete(url: str, model: str, question: str, max_tokens: int) -> tuple[str, dict]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Solve the math problem with concise reasoning. End with exactly '#### <number>'."},
            {"role": "user", "content": question + "\n/no_think"},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "seed": 0,
    }
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        result = json.loads(response.read().decode("utf-8"))
    return result["choices"][0]["message"]["content"], result.get("usage", {})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:11435/v1/chat/completions")
    parser.add_argument("--model", default="Qwen3-1.7B-Q4_K_M.gguf")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--max-tokens", type=int, default=128)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines()][:args.samples]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    details, started = [], time.perf_counter()
    for index, row in enumerate(rows):
        gold = extract_answer(row["answer"].split("####")[-1])
        item_started = time.perf_counter()
        text, usage = complete(args.url, args.model, row["question"], args.max_tokens)
        predicted = extract_answer(text)
        details.append({
            "index": index, "question": row["question"], "gold_answer": gold,
            "predicted_answer": predicted, "correct": predicted == gold,
            "response": text, "usage": usage,
            "latency_seconds": time.perf_counter() - item_started,
        })
        args.output.write_text(
            json.dumps({"status": "running", "completed": len(details), "samples": details}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[{index+1}/{len(rows)}] gold={gold} predicted={predicted} correct={predicted == gold}", flush=True)
    elapsed = time.perf_counter() - started
    summary = {
        "model": args.model, "runner": "llama.cpp OpenAI-compatible API",
        "sample_count": len(details), "max_tokens": args.max_tokens,
        "exact_match": sum(item["correct"] for item in details) / len(details),
        "correct_count": sum(item["correct"] for item in details),
        "elapsed_seconds": elapsed,
        "average_latency_seconds": elapsed / len(details),
    }
    args.output.write_text(json.dumps({"summary": summary, "samples": details}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
