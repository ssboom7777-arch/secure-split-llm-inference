from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import torch
from tokenizers import Tokenizer

from model import EnterpriseModel, SafeTensorReader
from protocol import payload_to_tensor, post_json, tensor_to_payload


MODEL: EnterpriseModel
TOKENIZER: Tokenizer
CLOUD_URLS: list[str]
NETWORK: dict
SERIALIZE_REQUESTS: bool = False
PREFILL_CHUNK_SIZE: int = 0
PREFILL_CHUNK_THRESHOLD: int = 0
REQUEST_LOCK = threading.Lock()


def format_prompt(prompt: str) -> str:
    return f"<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def cloud_url_for(request_id: str) -> str:
    try:
        index = uuid.UUID(request_id).int % len(CLOUD_URLS)
    except ValueError:
        index = sum(request_id.encode("utf-8")) % len(CLOUD_URLS)
    return CLOUD_URLS[index]


def cloud_forward(request_id: str, hidden: torch.Tensor, reset: bool) -> torch.Tensor:
    response = post_json(
        cloud_url_for(request_id) + "/forward",
        {
            "request_id": request_id,
            "reset": reset,
            "hidden": tensor_to_payload(hidden),
            "network": NETWORK,
        },
    )
    if "error" in response:
        raise RuntimeError(response["error"])
    return payload_to_tensor(response["hidden"])


def prefill(request_id: str, input_ids: list[int]) -> torch.Tensor:
    should_chunk = (
        PREFILL_CHUNK_SIZE > 0
        and PREFILL_CHUNK_THRESHOLD > 0
        and len(input_ids) > PREFILL_CHUNK_THRESHOLD
    )
    chunk_size = PREFILL_CHUNK_SIZE if should_chunk else len(input_ids)
    output = None
    for start in range(0, len(input_ids), chunk_size):
        chunk = torch.tensor([input_ids[start : start + chunk_size]], dtype=torch.long)
        output = cloud_forward(request_id, MODEL.embed(chunk), reset=start == 0)
    assert output is not None
    return output


def generate(prompt: str, max_new_tokens: int):
    request_id = str(uuid.uuid4())
    input_ids = TOKENIZER.encode(format_prompt(prompt), add_special_tokens=False).ids
    generated = []
    try:
        with torch.inference_mode():
            hidden = prefill(request_id, input_ids)
            for _ in range(max_new_tokens):
                logits = MODEL.logits(hidden[:, -1, :])
                token_id = int(torch.argmax(logits, dim=-1).item())
                if token_id == 2:
                    break
                generated.append(token_id)
                text = TOKENIZER.decode(generated, skip_special_tokens=True)
                yield text
                next_token = torch.tensor([[token_id]], dtype=torch.long)
                hidden = cloud_forward(request_id, MODEL.embed(next_token), reset=False)
    finally:
        try:
            post_json(cloud_url_for(request_id) + "/release", {"request_id": request_id}, timeout=10.0)
        except Exception:
            pass


def generate_complete(prompt: str, max_new_tokens: int, started_at: float | None = None) -> tuple[list[int], str, dict]:
    """Generate through the real HTTP split path and retain token IDs for verification."""
    request_id = str(uuid.uuid4())
    input_ids = TOKENIZER.encode(format_prompt(prompt), add_special_tokens=False).ids
    generated: list[int] = []
    started_at = time.perf_counter() if started_at is None else started_at
    token_times: list[float] = []
    try:
        with torch.inference_mode():
            hidden = prefill(request_id, input_ids)
            for _ in range(max_new_tokens):
                token_id = int(torch.argmax(MODEL.logits(hidden[:, -1, :]), dim=-1).item())
                if token_id == 2:
                    break
                generated.append(token_id)
                token_times.append(time.perf_counter())
                next_token = torch.tensor([[token_id]], dtype=torch.long)
                hidden = cloud_forward(request_id, MODEL.embed(next_token), reset=False)
    finally:
        try:
            post_json(cloud_url_for(request_id) + "/release", {"request_id": request_id}, timeout=10.0)
        except Exception:
            pass
    ttft = token_times[0] - started_at if token_times else time.perf_counter() - started_at
    gaps = [b - a for a, b in zip(token_times, token_times[1:])]
    timing = {
        "ttft_seconds": ttft,
        "mean_tpot_seconds": sum(gaps) / len(gaps) if gaps else 0.0,
        "max_tpot_seconds": max(gaps) if gaps else 0.0,
        "prefill_chunk_size": PREFILL_CHUNK_SIZE,
    }
    return generated, TOKENIZER.decode(generated, skip_special_tokens=True), timing


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("enterprise:", fmt % args, flush=True)

    def json_reply(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.json_reply(200, {"status": "ok", "service": "enterprise"})
        else:
            self.json_reply(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if self.path not in ("/generate", "/v1/chat/completions"):
                self.json_reply(404, {"error": "not found"})
                return
            if self.path == "/generate":
                prompt = payload["prompt"]
            else:
                prompt = payload["messages"][-1]["content"]
            max_tokens = int(payload.get("max_new_tokens", payload.get("max_tokens", 32)))
            stream = bool(payload.get("stream", True))
            if not stream:
                arrived_at = time.perf_counter()
                if SERIALIZE_REQUESTS:
                    with REQUEST_LOCK:
                        token_ids, text, timing = generate_complete(prompt, max_tokens, arrived_at)
                else:
                    token_ids, text, timing = generate_complete(prompt, max_tokens, arrived_at)
                self.json_reply(200, {
                    "text": text,
                    "token_ids": token_ids,
                    "generated_steps": len(token_ids),
                    "timing": timing,
                })
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            previous = ""
            for text in generate(prompt, max_tokens):
                delta = text[len(previous) :] if text.startswith(previous) else text
                previous = text
                event = {"choices": [{"delta": {"content": delta}}]}
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception as exc:
            self.json_reply(500, {"error": f"{type(exc).__name__}: {exc}"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--cloud-url", default="http://127.0.0.1:8101")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--network-one-way-ms", type=float, default=0.0)
    parser.add_argument("--bandwidth-gbps", type=float, default=0.0)
    parser.add_argument("--shared-link-url", default="")
    parser.add_argument("--serialize-requests", action="store_true")
    parser.add_argument("--prefill-chunk-size", type=int, default=0)
    parser.add_argument("--prefill-chunk-threshold", type=int, default=0)
    parser.add_argument("--torch-threads", type=int, default=0)
    args = parser.parse_args()
    global MODEL, TOKENIZER, CLOUD_URLS, NETWORK, SERIALIZE_REQUESTS, PREFILL_CHUNK_SIZE, PREFILL_CHUNK_THRESHOLD
    if args.torch_threads > 0:
        torch.set_num_threads(args.torch_threads)
        torch.set_num_interop_threads(1)
    print("enterprise: loading Embedding, Norm and LM Head...", flush=True)
    MODEL = EnterpriseModel(SafeTensorReader(args.weights))
    TOKENIZER = Tokenizer.from_file(str(args.tokenizer))
    CLOUD_URLS = [url.strip().rstrip("/") for url in args.cloud_url.split(",") if url.strip()]
    NETWORK = {
        "one_way_ms": args.network_one_way_ms,
        "bandwidth_gbps": args.bandwidth_gbps,
        "shared_link_url": args.shared_link_url,
    }
    SERIALIZE_REQUESTS = args.serialize_requests
    PREFILL_CHUNK_SIZE = args.prefill_chunk_size
    PREFILL_CHUNK_THRESHOLD = args.prefill_chunk_threshold
    print(f"enterprise: listening on http://{args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
