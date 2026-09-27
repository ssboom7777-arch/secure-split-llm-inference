from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn


CONFIG = {
    "vocab_size": 6400,
    "hidden_size": 768,
    "num_hidden_layers": 8,
    "num_attention_heads": 8,
    "num_key_value_heads": 4,
    "head_dim": 96,
    "intermediate_size": 2432,
    "max_position_embeddings": 32768,
    "rms_norm_eps": 1e-6,
    "rope_theta": 1e6,
}


class SafeTensorReader:
    def __init__(self, path: Path):
        self.path = path
        with path.open("rb") as handle:
            self.header_size = struct.unpack("<Q", handle.read(8))[0]
            self.header = json.loads(handle.read(self.header_size))
        self.data_start = 8 + self.header_size

    def tensor(self, name: str) -> torch.Tensor:
        meta = self.header[name]
        dtype = {"F16": np.dtype("<f2"), "F32": np.dtype("<f4")}[meta["dtype"]]
        begin, end = meta["data_offsets"]
        with self.path.open("rb") as handle:
            handle.seek(self.data_start + begin)
            raw = handle.read(end - begin)
        array = np.frombuffer(raw, dtype=dtype).reshape(meta["shape"]).astype(np.float32)
        return torch.from_numpy(array.copy())


class RMSNorm(nn.Module):
    def __init__(self, size: int, eps: float):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(size), requires_grad=False)
        self.eps = eps

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        normalized = x.float() * torch.rsqrt(x.float().pow(2).mean(-1, keepdim=True) + self.eps)
        return (normalized * self.weight).to(x.dtype)


def rope(position: int, length: int, device: torch.device) -> Tuple[torch.Tensor, torch.Tensor]:
    dim = CONFIG["head_dim"]
    inv = 1.0 / (CONFIG["rope_theta"] ** (torch.arange(0, dim, 2, device=device).float() / dim))
    positions = torch.arange(position, position + length, device=device).float()
    freqs = torch.outer(positions, inv)
    cos = torch.cat([freqs.cos(), freqs.cos()], dim=-1)
    sin = torch.cat([freqs.sin(), freqs.sin()], dim=-1)
    return cos, sin


def rotate_half(x: torch.Tensor) -> torch.Tensor:
    half = x.shape[-1] // 2
    return torch.cat([-x[..., half:], x[..., :half]], dim=-1)


def repeat_kv(x: torch.Tensor, repetitions: int) -> torch.Tensor:
    batch, length, heads, dim = x.shape
    if repetitions == 1:
        return x
    return x[:, :, :, None, :].expand(batch, length, heads, repetitions, dim).reshape(
        batch, length, heads * repetitions, dim
    )


class Attention(nn.Module):
    def __init__(self):
        super().__init__()
        hidden = CONFIG["hidden_size"]
        heads = CONFIG["num_attention_heads"]
        kv_heads = CONFIG["num_key_value_heads"]
        head_dim = CONFIG["head_dim"]
        self.heads = heads
        self.kv_heads = kv_heads
        self.head_dim = head_dim
        self.repetitions = heads // kv_heads
        self.q_proj = nn.Linear(hidden, heads * head_dim, bias=False)
        self.k_proj = nn.Linear(hidden, kv_heads * head_dim, bias=False)
        self.v_proj = nn.Linear(hidden, kv_heads * head_dim, bias=False)
        self.o_proj = nn.Linear(heads * head_dim, hidden, bias=False)
        self.q_norm = RMSNorm(head_dim, CONFIG["rms_norm_eps"])
        self.k_norm = RMSNorm(head_dim, CONFIG["rms_norm_eps"])

    def forward(
        self,
        x: torch.Tensor,
        start_pos: int,
        past: Optional[Tuple[torch.Tensor, torch.Tensor]],
    ) -> Tuple[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        batch, query_len, _ = x.shape
        q = self.q_proj(x).view(batch, query_len, self.heads, self.head_dim)
        k = self.k_proj(x).view(batch, query_len, self.kv_heads, self.head_dim)
        v = self.v_proj(x).view(batch, query_len, self.kv_heads, self.head_dim)
        q, k = self.q_norm(q), self.k_norm(k)
        cos, sin = rope(start_pos, query_len, x.device)
        q = q * cos[None, :, None, :] + rotate_half(q) * sin[None, :, None, :]
        k = k * cos[None, :, None, :] + rotate_half(k) * sin[None, :, None, :]
        if past is not None:
            k = torch.cat([past[0], k], dim=1)
            v = torch.cat([past[1], v], dim=1)
        present = (k.detach(), v.detach())
        q = q.transpose(1, 2)
        full_k = repeat_kv(k, self.repetitions).transpose(1, 2)
        full_v = repeat_kv(v, self.repetitions).transpose(1, 2)
        scores = (q @ full_k.transpose(-2, -1)) / math.sqrt(self.head_dim)
        key_positions = torch.arange(full_k.shape[-2], device=x.device)
        query_positions = start_pos + torch.arange(query_len, device=x.device)
        causal = key_positions[None, :] > query_positions[:, None]
        scores = scores.masked_fill(causal[None, None, :, :], float("-inf"))
        # RoPE may promote BF16 queries to FP32 on CPU.  Cast the softmax
        # probabilities to the value dtype so BF16 models remain executable;
        # this is a no-op for the original FP32 MiniMind path.
        output = F.softmax(scores.float(), dim=-1).to(full_v.dtype) @ full_v
        output = output.transpose(1, 2).reshape(batch, query_len, -1)
        return self.o_proj(output), present


class FeedForward(nn.Module):
    def __init__(self):
        super().__init__()
        hidden, intermediate = CONFIG["hidden_size"], CONFIG["intermediate_size"]
        self.gate_proj = nn.Linear(hidden, intermediate, bias=False)
        self.up_proj = nn.Linear(hidden, intermediate, bias=False)
        self.down_proj = nn.Linear(intermediate, hidden, bias=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.down_proj(F.silu(self.gate_proj(x)) * self.up_proj(x))


class Block(nn.Module):
    def __init__(self):
        super().__init__()
        self.input_layernorm = RMSNorm(CONFIG["hidden_size"], CONFIG["rms_norm_eps"])
        self.post_attention_layernorm = RMSNorm(CONFIG["hidden_size"], CONFIG["rms_norm_eps"])
        self.self_attn = Attention()
        self.mlp = FeedForward()

    def forward(self, x, start_pos, past):
        attention, present = self.self_attn(self.input_layernorm(x), start_pos, past)
        x = x + attention
        x = x + self.mlp(self.post_attention_layernorm(x))
        return x, present


def assign(parameter: nn.Parameter, value: torch.Tensor) -> None:
    parameter.data.copy_(value.to(parameter.dtype))
    parameter.requires_grad_(False)


class EnterpriseModel(nn.Module):
    def __init__(self, reader: SafeTensorReader):
        super().__init__()
        self.embedding = nn.Embedding(CONFIG["vocab_size"], CONFIG["hidden_size"])
        self.norm = RMSNorm(CONFIG["hidden_size"], CONFIG["rms_norm_eps"])
        assign(self.embedding.weight, reader.tensor("model.embed_tokens.weight"))
        assign(self.norm.weight, reader.tensor("model.norm.weight"))
        self.eval()

    def embed(self, token_ids: torch.Tensor) -> torch.Tensor:
        return self.embedding(token_ids)

    def logits(self, hidden: torch.Tensor) -> torch.Tensor:
        normalized = self.norm(hidden)
        return F.linear(normalized, self.embedding.weight)


class CloudModel(nn.Module):
    def __init__(self, reader: SafeTensorReader):
        super().__init__()
        self.layers = nn.ModuleList([Block() for _ in range(CONFIG["num_hidden_layers"])])
        for index, layer in enumerate(self.layers):
            prefix = f"model.layers.{index}."
            assign(layer.input_layernorm.weight, reader.tensor(prefix + "input_layernorm.weight"))
            assign(layer.post_attention_layernorm.weight, reader.tensor(prefix + "post_attention_layernorm.weight"))
            attention = layer.self_attn
            assign(attention.q_proj.weight, reader.tensor(prefix + "self_attn.q_proj.weight"))
            assign(attention.k_proj.weight, reader.tensor(prefix + "self_attn.k_proj.weight"))
            assign(attention.v_proj.weight, reader.tensor(prefix + "self_attn.v_proj.weight"))
            assign(attention.o_proj.weight, reader.tensor(prefix + "self_attn.o_proj.weight"))
            assign(attention.q_norm.weight, reader.tensor(prefix + "self_attn.q_norm.weight"))
            assign(attention.k_norm.weight, reader.tensor(prefix + "self_attn.k_norm.weight"))
            assign(layer.mlp.gate_proj.weight, reader.tensor(prefix + "mlp.gate_proj.weight"))
            assign(layer.mlp.up_proj.weight, reader.tensor(prefix + "mlp.up_proj.weight"))
            assign(layer.mlp.down_proj.weight, reader.tensor(prefix + "mlp.down_proj.weight"))
        self.caches: Dict[str, list] = {}
        self.eval()

    @torch.inference_mode()
    def forward_request(self, request_id: str, hidden: torch.Tensor, reset: bool = False) -> torch.Tensor:
        if reset or request_id not in self.caches:
            self.caches[request_id] = [None] * len(self.layers)
        cache = self.caches[request_id]
        start_pos = 0 if cache[0] is None else cache[0][0].shape[1]
        presents = []
        for layer, past in zip(self.layers, cache):
            hidden, present = layer(hidden, start_pos, past)
            presents.append(present)
        self.caches[request_id] = presents
        return hidden

    def release(self, request_id: str) -> None:
        self.caches.pop(request_id, None)

    @torch.inference_mode()
    def forward_batch(self, requests: list[tuple[str, torch.Tensor, bool]]) -> list[torch.Tensor]:
        """Execute compatible requests as one batch and preserve per-request KV caches."""
        if not requests:
            return []
        for request_id, _, reset in requests:
            if reset or request_id not in self.caches:
                self.caches[request_id] = [None] * len(self.layers)
        signatures = []
        for request_id, hidden, _ in requests:
            cache = self.caches[request_id]
            start = 0 if cache[0] is None else cache[0][0].shape[1]
            signatures.append((hidden.shape[1], start))
        if len(set(signatures)) != 1:
            raise ValueError(f"Incompatible batch shapes/cache positions: {signatures}")
        _, start_pos = signatures[0]
        hidden = torch.cat([item[1] for item in requests], dim=0)
        per_request_presents = [[] for _ in requests]
        for layer_index, layer in enumerate(self.layers):
            past_items = [self.caches[item[0]][layer_index] for item in requests]
            if all(item is None for item in past_items):
                past = None
            elif all(item is not None for item in past_items):
                past = (
                    torch.cat([item[0] for item in past_items], dim=0),
                    torch.cat([item[1] for item in past_items], dim=0),
                )
            else:
                raise ValueError("Mixed empty and non-empty KV caches in one batch")
            hidden, present = layer(hidden, start_pos, past)
            for index in range(len(requests)):
                per_request_presents[index].append(
                    (present[0][index : index + 1].detach(), present[1][index : index + 1].detach())
                )
        for index, (request_id, _, _) in enumerate(requests):
            self.caches[request_id] = per_request_presents[index]
        return [hidden[index : index + 1] for index in range(len(requests))]


class MonolithicModel(nn.Module):
    def __init__(self, reader: SafeTensorReader):
        super().__init__()
        self.enterprise = EnterpriseModel(reader)
        self.cloud = CloudModel(reader)

    @torch.inference_mode()
    def step(self, request_id: str, token_ids: torch.Tensor, reset: bool) -> torch.Tensor:
        hidden = self.enterprise.embed(token_ids)
        hidden = self.cloud.forward_request(request_id, hidden, reset=reset)
        return self.enterprise.logits(hidden[:, -1, :])
