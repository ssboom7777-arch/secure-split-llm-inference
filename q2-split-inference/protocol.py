from __future__ import annotations

import base64
import json
import urllib.request

import numpy as np
import torch


def tensor_to_payload(tensor: torch.Tensor) -> dict:
    array = tensor.detach().cpu().float().contiguous().numpy()
    return {
        "shape": list(array.shape),
        "dtype": "float32",
        "data": base64.b64encode(array.tobytes()).decode("ascii"),
    }


def payload_to_tensor(payload: dict) -> torch.Tensor:
    raw = base64.b64decode(payload["data"])
    array = np.frombuffer(raw, dtype=np.float32).reshape(payload["shape"]).copy()
    return torch.from_numpy(array)


def post_json(url: str, payload: dict, timeout: float = 300.0) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))
