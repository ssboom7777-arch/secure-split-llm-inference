#!/usr/bin/env python3
"""Recover MiniMind input tokens from transmitted embedding hidden states.

The script needs only NumPy.  It reads the embedding tensor directly from the
safetensors file, so PyTorch and Transformers are deliberately not required.
"""

from __future__ import annotations

import argparse
import json
import struct
import time
from pathlib import Path

import numpy as np


def bytes_to_unicode() -> dict[int, str]:
    """GPT-2/ByteLevel reversible byte-to-Unicode map."""
    visible = list(range(ord("!"), ord("~") + 1))
    visible += list(range(ord("¡"), ord("¬") + 1))
    visible += list(range(ord("®"), ord("ÿ") + 1))
    chars = visible[:]
    extra = 0
    for value in range(256):
        if value not in visible:
            visible.append(value)
            chars.append(256 + extra)
            extra += 1
    return dict(zip(visible, map(chr, chars)))


def load_vocab(tokenizer_path: Path) -> tuple[dict[str, int], dict[int, str]]:
    payload = json.loads(tokenizer_path.read_text(encoding="utf-8"))
    vocab = payload["model"]["vocab"]
    return vocab, {idx: token for token, idx in vocab.items()}


def encode_lossless_bytes(text: str, vocab: dict[str, int]) -> list[int]:
    """Encode text as valid ByteLevel base tokens without applying BPE merges."""
    mapping = bytes_to_unicode()
    tokens = [mapping[value] for value in text.encode("utf-8")]
    missing = [token for token in tokens if token not in vocab]
    if missing:
        raise ValueError(f"Tokenizer lacks {len(missing)} required byte tokens")
    return [vocab[token] for token in tokens]


def decode_lossless_bytes(token_ids: list[int], inverse_vocab: dict[int, str]) -> str:
    inverse_bytes = {char: value for value, char in bytes_to_unicode().items()}
    raw = bytearray()
    for token_id in token_ids:
        token = inverse_vocab[token_id]
        if len(token) != 1 or token not in inverse_bytes:
            return "<recovered token sequence contains merged/non-byte tokens>"
        raw.append(inverse_bytes[token])
    return raw.decode("utf-8", errors="replace")


def load_safetensor_tensor(path: Path, tensor_name: str) -> np.ndarray:
    dtype_map = {
        "F16": np.dtype("<f2"),
        "F32": np.dtype("<f4"),
        "I16": np.dtype("<i2"),
        "I32": np.dtype("<i4"),
    }
    with path.open("rb") as handle:
        header_size = struct.unpack("<Q", handle.read(8))[0]
        header = json.loads(handle.read(header_size))
        if tensor_name not in header:
            names = ", ".join(key for key in header if key != "__metadata__")
            raise KeyError(f"{tensor_name!r} not found; tensors: {names}")
        metadata = header[tensor_name]
        dtype = dtype_map[metadata["dtype"]]
        begin, end = metadata["data_offsets"]
        handle.seek(8 + header_size + begin)
        raw = handle.read(end - begin)
    return np.frombuffer(raw, dtype=dtype).reshape(metadata["shape"]).astype(np.float32)


def normalize(rows: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(rows, axis=-1, keepdims=True)
    return rows / np.maximum(norms, 1e-12)


def nearest_embedding(hidden: np.ndarray, normalized_table: np.ndarray) -> list[int]:
    scores = normalize(hidden) @ normalized_table.T
    return scores.argmax(axis=-1).astype(int).tolist()


def recovery_rate(expected: list[int], recovered: list[int]) -> float:
    return float(np.mean(np.asarray(expected) == np.asarray(recovered)))


def noise_experiment(
    clean_hidden: np.ndarray,
    token_ids: list[int],
    table: np.ndarray,
    normalized_table: np.ndarray,
    noise_ratios: list[float],
    rng: np.random.Generator,
) -> list[dict[str, float]]:
    per_dimension_scale = float(np.median(np.linalg.norm(table, axis=1)) / np.sqrt(table.shape[1]))
    clean_norm = normalize(clean_hidden)
    rows = []
    for ratio in noise_ratios:
        noisy = clean_hidden + rng.normal(
            0.0, ratio * per_dimension_scale, size=clean_hidden.shape
        ).astype(np.float32)
        guessed = nearest_embedding(noisy, normalized_table)
        cosine = float(np.mean(np.sum(clean_norm * normalize(noisy), axis=-1)))
        rows.append(
            {
                "noise_sigma_over_embedding_rms": ratio,
                "token_recovery_rate": recovery_rate(token_ids, guessed),
                "mean_clean_noisy_cosine": cosine,
            }
        )
    return rows


def secret_sharing_experiment(
    clean_hidden: np.ndarray,
    token_ids: list[int],
    normalized_table: np.ndarray,
    rng: np.random.Generator,
) -> dict[str, float]:
    """Two additive shares over a prime field; one share is uniformly random."""
    prime = 65521
    max_abs = float(np.max(np.abs(clean_hidden)))
    scale = min(8192.0, (prime // 2 - 1) / max(max_abs, 1e-9))
    quantized = np.rint(clean_hidden * scale).astype(np.int64)
    encoded = np.mod(quantized, prime)
    share_a = rng.integers(0, prime, size=encoded.shape, dtype=np.int64)
    share_b = np.mod(encoded - share_a, prime)

    def signed(values: np.ndarray) -> np.ndarray:
        values = values.copy()
        values[values > prime // 2] -= prime
        return values.astype(np.float32) / scale

    attacker_guess = nearest_embedding(signed(share_a), normalized_table)
    combined = np.mod(share_a + share_b, prime)
    reconstructed_guess = nearest_embedding(signed(combined), normalized_table)
    return {
        "single_cloud_token_recovery_rate": recovery_rate(token_ids, attacker_guess),
        "combined_shares_token_recovery_rate": recovery_rate(token_ids, reconstructed_guess),
        "quantization_scale": scale,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument(
        "--prompt",
        action="append",
        help="Prompt to test; repeat the flag for multiple prompts.",
    )
    parser.add_argument("--output", type=Path, default=Path("results.json"))
    parser.add_argument("--seed", type=int, default=20260921)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    started = time.perf_counter()
    vocab, inverse_vocab = load_vocab(args.tokenizer)
    embedding = load_safetensor_tensor(args.weights, "model.embed_tokens.weight")
    normalized_table = normalize(embedding)
    prompts = args.prompt or [
        "机密项目代号青鸟，预算为一千万元，请勿外传。",
        "客户张三的身份证号和账户余额属于敏感信息。",
        "The acquisition price is confidential until the board approves it.",
        "服务器地址为10.24.7.9，访问密钥不得写入日志。",
        "请总结这份尚未公开的季度财务报告。",
    ]
    prompt_token_ids = [encode_lossless_bytes(prompt, vocab) for prompt in prompts]
    token_ids = [token_id for ids in prompt_token_ids for token_id in ids]

    # This tensor is exactly what an embedding-only enterprise partition sends.
    transmitted_hidden = embedding[np.asarray(token_ids)]
    recovered_ids = nearest_embedding(transmitted_hidden, normalized_table)
    baseline_rate = recovery_rate(token_ids, recovered_ids)

    rng = np.random.default_rng(args.seed)
    noise_rows = noise_experiment(
        transmitted_hidden,
        token_ids,
        embedding,
        normalized_table,
        [0.0, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0],
        rng,
    )
    shares = secret_sharing_experiment(
        transmitted_hidden, token_ids, normalized_table, rng
    )

    recovered_prompts = []
    offset = 0
    for prompt, ids in zip(prompts, prompt_token_ids):
        recovered_slice = recovered_ids[offset : offset + len(ids)]
        recovered_prompts.append(
            {
                "prompt": prompt,
                "token_count": len(ids),
                "token_recovery_rate": recovery_rate(ids, recovered_slice),
                "recovered_text": decode_lossless_bytes(recovered_slice, inverse_vocab),
            }
        )
        offset += len(ids)

    result = {
        "model": "jingyaogong/minimind-3",
        "attack": "cosine nearest-neighbor over the known embedding table",
        "prompts": recovered_prompts,
        "token_count": len(token_ids),
        "hidden_size": int(embedding.shape[1]),
        "vocab_size": int(embedding.shape[0]),
        "baseline": {
            "token_recovery_rate": baseline_rate,
            "exact_sequence_recovered": token_ids == recovered_ids,
        },
        "gaussian_noise_tradeoff": noise_rows,
        "two_party_additive_secret_sharing": shares,
        "elapsed_seconds": time.perf_counter() - started,
        "limitations": [
            "ByteLevel base tokens are used without BPE merges; they decode losslessly to the prompt.",
            "Cosine similarity is a representation-distortion proxy, not an end-task accuracy metric.",
            "Secret sharing needs an MPC-capable cloud computation protocol; ordinary Transformer layers cannot consume one share alone.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    for item in recovered_prompts:
        print(f"prompt: {item['prompt']}")
        print(f"recovered: {item['recovered_text']}")
        print(f"recovery: {item['token_recovery_rate']:.2%}\n")
    print(f"baseline token recovery: {baseline_rate:.2%}")
    print("\nGaussian noise trade-off")
    print("sigma/RMS | recovery | clean-noisy cosine")
    for row in noise_rows:
        print(
            f"{row['noise_sigma_over_embedding_rms']:>9.2f} | "
            f"{row['token_recovery_rate']:>8.2%} | "
            f"{row['mean_clean_noisy_cosine']:>18.4f}"
        )
    print("\nTwo-party additive secret sharing")
    print(f"one cloud recovery: {shares['single_cloud_token_recovery_rate']:.2%}")
    print(f"combined recovery:  {shares['combined_shares_token_recovery_rate']:.2%}")
    print(f"results: {args.output.resolve()}")


if __name__ == "__main__":
    main()
