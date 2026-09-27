from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import torch
from tokenizers import Tokenizer


PROMPTS = [
    "Hello, how are you?",
    "What is the capital of China?",
    "Write one sentence about artificial intelligence.",
    "Summarize why data privacy matters.",
    "What is two plus three?",
    "中国的首都是哪里？",
    "请用一句话介绍人工智能。",
    "为什么企业需要保护机密数据？",
    "请写一句关于春天的话。",
    "太阳从哪个方向升起？",
]


def token_agreement(clean: list[int], noisy: list[int]) -> float:
    denominator = max(len(clean), len(noisy), 1)
    return sum(a == b for a, b in zip(clean, noisy)) / denominator


def write_svg(rows: list[dict], output: Path) -> None:
    width, height = 900, 520
    left, right, top, bottom = 80, 35, 45, 75
    plot_w, plot_h = width - left - right, height - top - bottom
    xs = [left + i * plot_w / (len(rows) - 1) for i in range(len(rows))]
    y = lambda value: top + (1.0 - value) * plot_h
    attack_points = " ".join(f"{x:.1f},{y(r['attack_token_recovery_rate']):.1f}" for x, r in zip(xs, rows))
    utility_points = " ".join(f"{x:.1f},{y(r['mean_output_token_agreement']):.1f}" for x, r in zip(xs, rows))
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">',
        '<title>MiniMind Gaussian-noise privacy and utility trade-off</title>',
        '<rect width="100%" height="100%" fill="white"/>',
        '<style>text{font-family:Segoe UI,Arial,sans-serif;fill:#222}.axis{stroke:#555;stroke-width:1}.grid{stroke:#ddd;stroke-width:1}.attack{stroke:#d1495b;fill:none;stroke-width:3}.utility{stroke:#0077b6;fill:none;stroke-width:3}.dot-a{fill:#d1495b}.dot-u{fill:#0077b6}</style>',
        '<text x="450" y="25" text-anchor="middle" font-size="18" font-weight="600">MiniMind noise: privacy gain versus generation utility</text>',
    ]
    for tick in range(0, 101, 20):
        yy = y(tick / 100)
        parts.append(f'<line class="grid" x1="{left}" y1="{yy:.1f}" x2="{width-right}" y2="{yy:.1f}"/>')
        parts.append(f'<text x="{left-12}" y="{yy+5:.1f}" text-anchor="end" font-size="13">{tick}%</text>')
    parts.extend([
        f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}"/>',
        f'<line class="axis" x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}"/>',
        f'<polyline class="attack" points="{attack_points}"/>',
        f'<polyline class="utility" points="{utility_points}"/>',
    ])
    for x, row in zip(xs, rows):
        ratio = row["noise_sigma_over_embedding_rms"]
        parts.append(f'<circle class="dot-a" cx="{x:.1f}" cy="{y(row["attack_token_recovery_rate"]):.1f}" r="5"/>')
        parts.append(f'<circle class="dot-u" cx="{x:.1f}" cy="{y(row["mean_output_token_agreement"]):.1f}" r="5"/>')
        parts.append(f'<text x="{x:.1f}" y="{height-bottom+25}" text-anchor="middle" font-size="13">{ratio:g}</text>')
    parts.extend([
        f'<text x="{left+plot_w/2:.1f}" y="{height-18}" text-anchor="middle" font-size="14">Noise sigma / embedding RMS</text>',
        f'<text x="18" y="{top+plot_h/2:.1f}" text-anchor="middle" font-size="14" transform="rotate(-90 18 {top+plot_h/2:.1f})">Rate</text>',
        '<line class="attack" x1="560" y1="48" x2="600" y2="48"/><text x="610" y="53" font-size="13">Attack token recovery</text>',
        '<line class="utility" x1="560" y1="72" x2="600" y2="72"/><text x="610" y="77" font-size="13">Output token agreement</text>',
        '</svg>',
    ])
    output.write_text("\n".join(parts), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--q2-dir", type=Path, required=True)
    parser.add_argument("--attack-results", type=Path, required=True)
    parser.add_argument("--max-new-tokens", type=int, default=32)
    parser.add_argument("--seed", type=int, default=20260925)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chart", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.q2_dir.resolve()))
    from enterprise_server import format_prompt
    from model import CloudModel, EnterpriseModel, SafeTensorReader

    tokenizer = Tokenizer.from_file(str(args.tokenizer))
    reader = SafeTensorReader(args.weights)
    enterprise, cloud = EnterpriseModel(reader), CloudModel(reader)
    table = enterprise.embedding.weight.detach()
    rms = float(torch.median(torch.linalg.vector_norm(table, dim=1)) / table.shape[1] ** 0.5)

    def generate(prompt: str, ratio: float, prompt_index: int) -> list[int]:
        input_ids = tokenizer.encode(format_prompt(prompt), add_special_tokens=False).ids
        request_id = f"generation-{ratio}-{prompt_index}"
        generated: list[int] = []
        rng = torch.Generator().manual_seed(args.seed + prompt_index * 1009 + round(ratio * 100))

        def noisy(hidden: torch.Tensor) -> torch.Tensor:
            if ratio == 0:
                return hidden
            return hidden + torch.randn(hidden.shape, generator=rng, dtype=hidden.dtype) * ratio * rms

        with torch.inference_mode():
            hidden = cloud.forward_request(
                request_id, noisy(enterprise.embed(torch.tensor([input_ids], dtype=torch.long))), True
            )
            for _ in range(args.max_new_tokens):
                token = int(torch.argmax(enterprise.logits(hidden[:, -1, :]), dim=-1).item())
                if token == 2:
                    break
                generated.append(token)
                hidden = cloud.forward_request(
                    request_id,
                    noisy(enterprise.embed(torch.tensor([[token]], dtype=torch.long))),
                    False,
                )
        cloud.release(request_id)
        return generated

    started = time.perf_counter()
    clean = [generate(prompt, 0, index) for index, prompt in enumerate(PROMPTS)]
    attack = json.loads(args.attack_results.read_text(encoding="utf-8"))
    attack_rows = attack["gaussian_noise_tradeoff"]
    tradeoff, details = [], []
    for attack_row in attack_rows:
        ratio = float(attack_row["noise_sigma_over_embedding_rms"])
        generated = clean if ratio == 0 else [generate(p, ratio, i) for i, p in enumerate(PROMPTS)]
        agreements = [token_agreement(a, b) for a, b in zip(clean, generated)]
        exact = [a == b for a, b in zip(clean, generated)]
        first = [bool(a and b and a[0] == b[0]) for a, b in zip(clean, generated)]
        row = {
            "noise_sigma_over_embedding_rms": ratio,
            "attack_token_recovery_rate": attack_row["token_recovery_rate"],
            "mean_output_token_agreement": sum(agreements) / len(agreements),
            "first_output_token_match_rate": sum(first) / len(first),
            "exact_output_sequence_match_rate": sum(exact) / len(exact),
        }
        tradeoff.append(row)
        details.append({
            "ratio": ratio,
            "prompts": [
                {"prompt": p, "clean_text": tokenizer.decode(a), "noisy_text": tokenizer.decode(b), "token_agreement": score}
                for p, a, b, score in zip(PROMPTS, clean, generated, agreements)
            ],
        })
        print(f"sigma={ratio:>4g} recovery={row['attack_token_recovery_rate']:.1%} output_agreement={row['mean_output_token_agreement']:.1%}")
    result = {
        "model": "jingyaogong/minimind-3",
        "prompt_count": len(PROMPTS),
        "max_new_tokens": args.max_new_tokens,
        "noise_application": "independent Gaussian noise on every transmitted prefill/decode embedding",
        "tradeoff": tradeoff,
        "details": details,
        "elapsed_seconds": time.perf_counter() - started,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    write_svg(tradeoff, args.chart)
    print(f"results: {args.output.resolve()}")
    print(f"chart: {args.chart.resolve()}")


if __name__ == "__main__":
    main()
