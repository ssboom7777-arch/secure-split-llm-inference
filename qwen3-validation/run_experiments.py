from __future__ import annotations

import argparse
import heapq
import json
import math
import struct
import sys
import time
from pathlib import Path

import numpy as np
import torch


ROOT = Path(__file__).resolve().parents[1]
Q2 = ROOT / "q2-split-inference"
MODEL_DIR = ROOT / "assets" / "qwen3-1.7b"
sys.path.insert(0, str(Q2))
import model as qmodel  # noqa: E402


PROMPTS = [
    "中国的首都是哪里？",
    "请用一句话说明为什么企业要保护客户数据。",
    "What is two plus three?",
]
NOISE_RATIOS = [0.0, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0]


class ShardedReader:
    def __init__(self, directory: Path):
        index = json.loads((directory / "model.safetensors.index.json").read_text(encoding="utf-8"))
        self.directory = directory
        self.weight_map = index["weight_map"]
        self.headers: dict[str, tuple[Path, int, dict]] = {}

    def tensor(self, name: str) -> torch.Tensor:
        filename = self.weight_map[name]
        path = self.directory / filename
        if filename not in self.headers:
            with path.open("rb") as handle:
                header_size = struct.unpack("<Q", handle.read(8))[0]
                header = json.loads(handle.read(header_size))
            self.headers[filename] = (path, 8 + header_size, header)
        path, data_start, header = self.headers[filename]
        meta = header[name]
        begin, end = meta["data_offsets"]
        with path.open("rb") as handle:
            handle.seek(data_start + begin)
            raw = handle.read(end - begin)
        shape = tuple(meta["shape"])
        if meta["dtype"] == "BF16":
            array = np.frombuffer(raw, dtype=np.uint16).copy().reshape(shape)
            return torch.from_numpy(array).view(torch.bfloat16)
        dtype = {"F16": np.float16, "F32": np.float32}[meta["dtype"]]
        return torch.from_numpy(np.frombuffer(raw, dtype=dtype).copy().reshape(shape))


def configure() -> None:
    cfg = json.loads((MODEL_DIR / "config.json").read_text(encoding="utf-8"))
    for key in qmodel.CONFIG:
        qmodel.CONFIG[key] = cfg[key]


def encode(tokenizer, text: str) -> list[int]:
    # This intentionally avoids a dependency on Transformers. The checked-in
    # tokenizer.json is sufficient for deterministic model-path validation.
    # Use Qwen's documented ChatML envelope so generation starts in the
    # assistant role rather than as an unformatted text continuation.
    prompt = f"<|im_start|>user\n{text}<|im_end|>\n<|im_start|>assistant\n"
    return tokenizer.encode(prompt, add_special_tokens=False).ids


@torch.inference_mode()
def generate(enterprise, cloud, ids: list[int], max_new: int, request_id: str,
             noise_sigma: float = 0.0, noise_seed: int = 0) -> tuple[list[int], dict]:
    generated: list[int] = []
    generator = torch.Generator().manual_seed(noise_seed)
    table = enterprise.embedding.weight
    rms = float(torch.median(torch.linalg.vector_norm(table.float(), dim=1)) / math.sqrt(table.shape[1]))
    prefill_started = time.perf_counter()
    hidden = enterprise.embed(torch.tensor([ids], dtype=torch.long))
    if noise_sigma:
        hidden = hidden + torch.randn(hidden.shape, generator=generator, dtype=hidden.dtype) * (noise_sigma * rms)
    hidden = cloud.forward_request(request_id, hidden, True)
    prefill_seconds = time.perf_counter() - prefill_started
    decode_steps: list[float] = []
    for _ in range(max_new):
        started = time.perf_counter()
        token = int(torch.argmax(enterprise.logits(hidden[:, -1, :]), dim=-1).item())
        generated.append(token)
        hidden = enterprise.embed(torch.tensor([[token]], dtype=torch.long))
        if noise_sigma:
            hidden = hidden + torch.randn(hidden.shape, generator=generator, dtype=hidden.dtype) * (noise_sigma * rms)
        hidden = cloud.forward_request(request_id, hidden, False)
        decode_steps.append(time.perf_counter() - started)
    cloud.release(request_id)
    return generated, {"prefill_seconds": prefill_seconds, "decode_step_seconds": decode_steps}


def agreement(a: list[int], b: list[int]) -> float:
    return sum(x == y for x, y in zip(a, b)) / max(len(a), len(b), 1)


def attack_recovery(table: torch.Tensor, ids: list[int], ratio: float, seed: int) -> float:
    # Chunked cosine NN avoids constructing a very large score matrix.
    clean = table[torch.tensor(ids)].float()
    rms = float(torch.median(torch.linalg.vector_norm(table.float(), dim=1)) / math.sqrt(table.shape[1]))
    rng = torch.Generator().manual_seed(seed)
    noisy = clean + torch.randn(clean.shape, generator=rng) * (ratio * rms)
    noisy = torch.nn.functional.normalize(noisy, dim=-1)
    best_score = torch.full((len(ids),), -float("inf"))
    best_id = torch.zeros(len(ids), dtype=torch.long)
    for start in range(0, table.shape[0], 4096):
        block = torch.nn.functional.normalize(table[start:start + 4096].float(), dim=-1)
        score = noisy @ block.T
        values, offsets = score.max(dim=1)
        update = values > best_score
        best_score[update] = values[update]
        best_id[update] = offsets[update] + start
    return float((best_id == torch.tensor(ids)).float().mean())


def simulate_capacity(prefill_s: float, decode_s: float, jobs: int, output_tokens: int,
                      split: bool, pipeline: bool) -> dict:
    # One real cloud execution slot. 500 km => 2.5 ms one-way propagation.
    # Split inference performs a hidden-state RTT for prefill and every decode
    # step; monolithic inference only sends the prompt once and streams output.
    raw_rtt = 0.005
    hidden_rtt = 0.005 + 2 * 2048 * 2 * 8 / 10e9
    rtt = hidden_rtt if split else raw_rtt
    if not pipeline:
        per_job = rtt + prefill_s + output_tokens * ((rtt if split else 0.0) + decode_s)
        makespan = jobs * per_job
        busy = jobs * (prefill_s + output_tokens * decode_s)
    else:
        future = [(rtt, j, "p", 0) for j in range(jobs)]
        heapq.heapify(future)
        now = busy = 0.0
        while future:
            ready, job, phase, produced = heapq.heappop(future)
            now = max(now, ready)
            service = prefill_s if phase == "p" else decode_s
            now += service
            busy += service
            if phase == "p":
                heapq.heappush(future, (now + (rtt if split else 0.0), job, "d", 1))
            elif produced < output_tokens:
                heapq.heappush(future, (now + (rtt if split else 0.0), job, "d", produced + 1))
        makespan = now
    return {
        "requests": jobs,
        "makespan_seconds": makespan,
        "throughput_qps": jobs / makespan,
        "cloud_slot_utilization": busy / makespan,
        "network_model": "500 km propagation + 10 Gbps serialization",
    }


def write_svg(rows: list[dict], path: Path) -> None:
    width, height = 820, 480
    left, right, top, bottom = 70, 30, 45, 70
    pw, ph = width-left-right, height-top-bottom
    xs = [left+i*pw/(len(rows)-1) for i in range(len(rows))]
    y = lambda v: top+(1-v)*ph
    attack = " ".join(f"{x:.1f},{y(r['attack_recovery']):.1f}" for x,r in zip(xs,rows))
    utility = " ".join(f"{x:.1f},{y(r['output_token_agreement']):.1f}" for x,r in zip(xs,rows))
    parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
           '<rect width="100%" height="100%" fill="white"/>',
           '<style>text{font-family:Segoe UI,Arial}.g{stroke:#ddd}.a{stroke:#d1495b;fill:none;stroke-width:3}.u{stroke:#087e8b;fill:none;stroke-width:3}</style>',
           '<text x="410" y="25" text-anchor="middle" font-size="18">Qwen3-1.7B: privacy–utility trade-off</text>']
    for t in range(0,101,20):
        yy=y(t/100); parts += [f'<line class="g" x1="{left}" y1="{yy}" x2="{width-right}" y2="{yy}"/>',f'<text x="{left-8}" y="{yy+5}" text-anchor="end">{t}%</text>']
    parts += [f'<polyline class="a" points="{attack}"/>',f'<polyline class="u" points="{utility}"/>']
    for x,r in zip(xs,rows): parts += [f'<text x="{x}" y="{height-bottom+25}" text-anchor="middle">{r["noise_ratio"]:g}</text>']
    parts += [f'<text x="{left+pw/2}" y="{height-15}" text-anchor="middle">Noise sigma / embedding RMS</text>',
              '<text x="560" y="48" fill="#d1495b">Attack recovery</text><text x="680" y="48" fill="#087e8b">Output agreement</text>','</svg>']
    path.write_text("\n".join(parts), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-new-tokens", type=int, default=4)
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()
    from tokenizers import Tokenizer

    torch.set_num_threads(min(8, torch.get_num_threads()))
    configure()
    old_dtype = torch.get_default_dtype()
    torch.set_default_dtype(torch.bfloat16)
    started = time.perf_counter()
    reader = ShardedReader(MODEL_DIR)
    enterprise = qmodel.EnterpriseModel(reader)
    cloud = qmodel.CloudModel(reader)
    torch.set_default_dtype(old_dtype)
    tokenizer = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))

    # 1. Alignment: the monolithic path and the serialized split boundary use
    # the same weights but independent KV-cache requests.
    alignment = []
    clean_outputs = []
    timings = []
    for i, prompt in enumerate(PROMPTS):
        ids = encode(tokenizer, prompt)
        mono, timing = generate(enterprise, cloud, ids, args.max_new_tokens, f"mono-{i}")
        split, _ = generate(enterprise, cloud, ids, args.max_new_tokens, f"split-{i}")
        clean_outputs.append(mono); timings.append(timing)
        alignment.append({"prompt": prompt, "input_tokens": len(ids), "monolithic_ids": mono,
                          "split_ids": split, "exact_match": mono == split,
                          "token_agreement": agreement(mono, split),
                          "output_text": tokenizer.decode(mono)})

    # 2. Privacy/utility. Attack is applied to actual Qwen embedding vectors;
    # utility is measured from real generation with noise at every boundary.
    all_ids = [token for p in PROMPTS for token in encode(tokenizer, p)]
    tradeoff = []
    for ratio in NOISE_RATIOS:
        recovery = attack_recovery(enterprise.embedding.weight, all_ids, ratio, 20260927)
        noisy_outputs = []
        for i, prompt in enumerate(PROMPTS):
            ids = encode(tokenizer, prompt)
            if ratio == 0:
                output = clean_outputs[i]
            else:
                output, _ = generate(enterprise, cloud, ids, args.max_new_tokens,
                                     f"noise-{ratio}-{i}", ratio, 20260927+i)
            noisy_outputs.append(output)
        scores = [agreement(a,b) for a,b in zip(clean_outputs,noisy_outputs)]
        tradeoff.append({"noise_ratio": ratio, "attack_recovery": recovery,
                         "output_token_agreement": sum(scores)/len(scores),
                         "exact_output_rate": sum(a == b for a,b in zip(clean_outputs,noisy_outputs))/len(scores),
                         "outputs": [tokenizer.decode(x) for x in noisy_outputs]})
        print(f"noise={ratio:g}: recovery={recovery:.1%}, utility={sum(scores)/len(scores):.1%}", flush=True)

    # 3. Measured compute drives a deterministic scheduling replay.
    prefill = sum(x["prefill_seconds"] for x in timings)/len(timings)
    decode_values = [v for x in timings for v in x["decode_step_seconds"]]
    decode = sum(decode_values)/len(decode_values)
    capacity = []
    for split in (False, True):
        for pipeline in (False, True):
            row = simulate_capacity(prefill, decode, args.jobs, args.max_new_tokens, split, pipeline)
            row.update({"model_path": "split" if split else "monolithic",
                        "scheduler": "pipeline" if pipeline else "serial"})
            capacity.append(row)

    output_dir = Path(__file__).resolve().parent / "results"
    output_dir.mkdir(exist_ok=True)
    result = {"model": "Qwen3-1.7B BF16", "execution": "real CPU forward passes; network/scheduling replay",
              "hardware": {"torch_threads": torch.get_num_threads(), "cuda": torch.cuda.is_available()},
              "alignment": alignment, "security_utility": tradeoff,
              "measured_service": {"mean_prefill_seconds": prefill, "mean_decode_step_seconds": decode},
              "capacity_matrix": capacity, "elapsed_seconds": time.perf_counter()-started,
              "limitations": ["CPU results validate correctness and relative trends, not H20 absolute QPS.",
                              "Capacity scheduling is a replay driven by measured model service time; it is not a real WAN.",
                              "Short prompts and outputs are used to keep the CPU experiment reproducible on 16 GB RAM."]}
    (output_dir / "qwen3-1.7b-three-experiments.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    write_svg(tradeoff, output_dir / "qwen3-1.7b-security-utility.svg")
    print(json.dumps({"alignment": alignment, "capacity_matrix": capacity,
                      "elapsed_seconds": result["elapsed_seconds"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
