from __future__ import annotations

import argparse
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import torch

from model import CloudModel, SafeTensorReader
from protocol import payload_to_tensor, post_json, tensor_to_payload


MODEL: CloudModel
SCHEDULER: "BatchScheduler"
MODEL_LOCK = threading.Lock()


@dataclass
class Job:
    request_id: str
    hidden: torch.Tensor
    reset: bool
    enqueued_at: float = field(default_factory=time.perf_counter)
    event: threading.Event = field(default_factory=threading.Event)
    result: torch.Tensor | None = None
    error: Exception | None = None


class BatchScheduler:
    def __init__(self, model: CloudModel, max_batch_size: int, batch_window_ms: float, policy: str,
                 max_decode_burst: int, prefill_starvation_ms: float):
        self.model = model
        self.max_batch_size = max_batch_size
        self.batch_window_seconds = batch_window_ms / 1000.0
        self.policy = policy
        self.max_decode_burst = max_decode_burst
        self.prefill_starvation_seconds = prefill_starvation_ms / 1000.0
        self.consecutive_decode_batches = 0
        self.jobs: queue.Queue[Job] = queue.Queue()
        self.pending: list[Job] = []
        self.metrics_lock = threading.Lock()
        self.reset_metrics()
        threading.Thread(target=self._run, daemon=True, name="cloud-batch-scheduler").start()

    def reset_metrics(self):
        with self.metrics_lock:
            self.started_at = time.perf_counter()
            self.busy_seconds = 0.0
            self.batch_sizes = []
            self.jobs_completed = 0
            self.prefill_jobs = 0
            self.decode_jobs = 0
            self.total_queue_wait_seconds = 0.0
            self.phase_queue_wait_seconds = {"prefill": 0.0, "decode": 0.0}
            self.phase_busy_seconds = {"prefill": 0.0, "decode": 0.0}
            self.network_bytes = {"prefill": 0, "decode": 0}
            self.network_delay_seconds = {"prefill": 0.0, "decode": 0.0}

    def submit(self, job: Job) -> torch.Tensor:
        self.jobs.put(job)
        job.event.wait()
        if job.error:
            raise job.error
        assert job.result is not None
        return job.result

    def _key(self, job: Job):
        if job.reset or job.request_id not in self.model.caches:
            start_pos = 0
        else:
            cache = self.model.caches[job.request_id]
            start_pos = 0 if cache[0] is None else cache[0][0].shape[1]
        return job.hidden.shape[1], start_pos

    def _collect(self) -> list[Job]:
        candidates = list(self.pending)
        self.pending = []
        if not candidates:
            candidates.append(self.jobs.get())
        while True:
            try:
                candidates.append(self.jobs.get_nowait())
            except queue.Empty:
                break
        deadline = time.perf_counter() + self.batch_window_seconds
        while self.batch_window_seconds > 0:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            try:
                candidates.append(self.jobs.get(timeout=remaining))
            except queue.Empty:
                break
        seed_index = 0
        if self.policy == "decode-first":
            seed_index = next((i for i, job in enumerate(candidates) if job.hidden.shape[1] == 1), 0)
        elif self.policy == "slo-aware":
            now = time.perf_counter()
            prefill_indexes = [i for i, job in enumerate(candidates) if job.hidden.shape[1] > 1]
            decode_indexes = [i for i, job in enumerate(candidates) if job.hidden.shape[1] == 1]
            oldest_prefill = min(prefill_indexes, key=lambda i: candidates[i].enqueued_at) if prefill_indexes else None
            prefill_starved = (
                oldest_prefill is not None
                and now - candidates[oldest_prefill].enqueued_at >= self.prefill_starvation_seconds
            )
            if oldest_prefill is not None and (
                prefill_starved or self.consecutive_decode_batches >= self.max_decode_burst or not decode_indexes
            ):
                seed_index = oldest_prefill
            elif decode_indexes:
                seed_index = min(decode_indexes, key=lambda i: candidates[i].enqueued_at)
        seed = candidates.pop(seed_index)
        key = self._key(seed)
        batch = [seed]
        remaining_jobs = []
        for job in candidates:
            if len(batch) < self.max_batch_size and self._key(job) == key:
                batch.append(job)
            else:
                remaining_jobs.append(job)
        self.pending.extend(remaining_jobs)
        return batch

    def _run(self):
        while True:
            batch = self._collect()
            begin = time.perf_counter()
            try:
                with MODEL_LOCK:
                    outputs = self.model.forward_batch(
                        [(job.request_id, job.hidden, job.reset) for job in batch]
                    )
                for job, output in zip(batch, outputs):
                    job.result = output
            except Exception as exc:
                for job in batch:
                    job.error = exc
            elapsed = time.perf_counter() - begin
            if batch[0].hidden.shape[1] == 1:
                self.consecutive_decode_batches += 1
            else:
                self.consecutive_decode_batches = 0
            with self.metrics_lock:
                self.busy_seconds += elapsed
                self.batch_sizes.append(len(batch))
                self.jobs_completed += len(batch)
                self.prefill_jobs += sum(job.hidden.shape[1] > 1 for job in batch)
                self.decode_jobs += sum(job.hidden.shape[1] == 1 for job in batch)
                self.total_queue_wait_seconds += sum(begin - job.enqueued_at for job in batch)
                phase = "prefill" if batch[0].hidden.shape[1] > 1 else "decode"
                self.phase_busy_seconds[phase] += elapsed
                self.phase_queue_wait_seconds[phase] += sum(begin - job.enqueued_at for job in batch)
            for job in batch:
                job.event.set()

    def metrics(self):
        with self.metrics_lock:
            wall = max(time.perf_counter() - self.started_at, 1e-9)
            sizes = list(self.batch_sizes)
            return {
                "wall_seconds": wall,
                "busy_seconds": self.busy_seconds,
                "busy_ratio": min(self.busy_seconds / wall, 1.0),
                "jobs_completed": self.jobs_completed,
                "prefill_jobs": self.prefill_jobs,
                "decode_jobs": self.decode_jobs,
                "forward_batches": len(sizes),
                "average_batch_size": sum(sizes) / len(sizes) if sizes else 0.0,
                "max_observed_batch_size": max(sizes) if sizes else 0,
                "configured_max_batch_size": self.max_batch_size,
                "batch_window_ms": self.batch_window_seconds * 1000.0,
                "scheduling_policy": self.policy,
                "max_decode_burst": self.max_decode_burst,
                "prefill_starvation_ms": self.prefill_starvation_seconds * 1000.0,
                "average_queue_wait_ms": (
                    self.total_queue_wait_seconds / self.jobs_completed * 1000.0
                    if self.jobs_completed else 0.0
                ),
                "prefill_average_queue_wait_ms": (
                    self.phase_queue_wait_seconds["prefill"] / self.prefill_jobs * 1000.0
                    if self.prefill_jobs else 0.0
                ),
                "decode_average_queue_wait_ms": (
                    self.phase_queue_wait_seconds["decode"] / self.decode_jobs * 1000.0
                    if self.decode_jobs else 0.0
                ),
                "prefill_busy_seconds": self.phase_busy_seconds["prefill"],
                "decode_busy_seconds": self.phase_busy_seconds["decode"],
                "network_bytes": dict(self.network_bytes),
                "simulated_network_delay_seconds": dict(self.network_delay_seconds),
            }

    def record_network(self, phase: str, raw_bytes: int, delay_seconds: float):
        with self.metrics_lock:
            self.network_bytes[phase] += raw_bytes
            self.network_delay_seconds[phase] += delay_seconds


def simulate_one_way(raw_bytes: int, network: dict, direction: str):
    link_url = str(network.get("shared_link_url", "")).rstrip("/")
    if link_url:
        result = post_json(link_url + "/transfer", {"direction": direction, "bytes": raw_bytes}, timeout=300.0)
        if "error" in result:
            raise RuntimeError(result["error"])
        return float(result["delay_seconds"])
    propagation_ms = float(network.get("one_way_ms", 0.0))
    bandwidth_gbps = float(network.get("bandwidth_gbps", 0.0))
    serialization_ms = 0.0 if bandwidth_gbps <= 0 else raw_bytes * 8.0 / (bandwidth_gbps * 1e9) * 1000.0
    delay = (propagation_ms + serialization_ms) / 1000.0
    if delay > 0:
        time.sleep(delay)
    return delay


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("cloud:", fmt % args, flush=True)

    def reply(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"status": "ok", "service": "cloud"})
        elif self.path == "/metrics":
            self.reply(200, SCHEDULER.metrics())
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if self.path == "/forward":
                hidden = payload_to_tensor(payload["hidden"])
                network = payload.get("network", {})
                phase = "prefill" if hidden.shape[1] > 1 else "decode"
                input_bytes = hidden.numel() * hidden.element_size()
                input_delay = simulate_one_way(input_bytes, network, "uplink")
                output = SCHEDULER.submit(Job(payload["request_id"], hidden, bool(payload.get("reset", False))))
                output_bytes = output.numel() * output.element_size()
                output_delay = simulate_one_way(output_bytes, network, "downlink")
                SCHEDULER.record_network(phase, input_bytes + output_bytes, input_delay + output_delay)
                self.reply(200, {"hidden": tensor_to_payload(output)})
            elif self.path == "/release":
                with MODEL_LOCK:
                    MODEL.release(payload["request_id"])
                self.reply(200, {"released": True})
            elif self.path == "/metrics/reset":
                SCHEDULER.reset_metrics()
                self.reply(200, {"reset": True})
            else:
                self.reply(404, {"error": "not found"})
        except Exception as exc:
            self.reply(500, {"error": f"{type(exc).__name__}: {exc}"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--max-batch-size", type=int, default=1)
    parser.add_argument("--batch-window-ms", type=float, default=0.0)
    parser.add_argument("--scheduling-policy", choices=("fifo", "decode-first", "slo-aware"), default="fifo")
    parser.add_argument("--max-decode-burst", type=int, default=4)
    parser.add_argument("--prefill-starvation-ms", type=float, default=500.0)
    parser.add_argument("--torch-threads", type=int, default=0)
    args = parser.parse_args()
    global MODEL, SCHEDULER
    if args.torch_threads > 0:
        torch.set_num_threads(args.torch_threads)
        torch.set_num_interop_threads(1)
    print("cloud: loading Transformer blocks...", flush=True)
    MODEL = CloudModel(SafeTensorReader(args.weights))
    SCHEDULER = BatchScheduler(
        MODEL, args.max_batch_size, args.batch_window_ms, args.scheduling_policy,
        args.max_decode_burst, args.prefill_starvation_ms,
    )
    print(
        f"cloud: listening on http://{args.host}:{args.port}; "
        f"max_batch={args.max_batch_size}, window={args.batch_window_ms}ms, policy={args.scheduling_policy}",
        flush=True,
    )
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
