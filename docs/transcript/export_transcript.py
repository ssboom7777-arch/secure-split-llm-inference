"""Export the visible user/assistant conversation from Codex rollout JSONL files."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path


HIDDEN_BLOCKS = (
    "recommended_plugins",
    "environment_context",
    "skills_instructions",
    "permissions",
    "collaboration_mode",
    "apps_instructions",
    "plugins_instructions",
    "multi_agent_mode",
)


def clean_visible_text(text: str) -> str:
    """Remove product-injected context while preserving the visible message."""
    for tag in HIDDEN_BLOCKS:
        text = re.sub(
            rf"<{tag}(?:\s[^>]*)?>.*?</{tag}>",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
    return text.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def collect_messages(paths: list[Path]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    for source_order, path in enumerate(paths):
        with path.open("r", encoding="utf-8") as stream:
            for line_order, line in enumerate(stream):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "response_item":
                    continue
                payload = event.get("payload", {})
                if payload.get("type") != "message":
                    continue
                role = payload.get("role")
                if role not in {"user", "assistant"}:
                    continue

                message_id = payload.get("id")
                if message_id and message_id in seen_ids:
                    continue
                if message_id:
                    seen_ids.add(message_id)

                parts = []
                for item in payload.get("content", []):
                    if item.get("type") in {"input_text", "output_text", "text"}:
                        visible = clean_visible_text(item.get("text", ""))
                        if visible:
                            parts.append(visible)
                text = "\n\n".join(parts).strip()
                if not text:
                    continue

                timestamp = event.get("timestamp", "")
                messages.append(
                    {
                        "timestamp": timestamp,
                        "role": role,
                        "text": text,
                        "phase": payload.get("phase", ""),
                        "source_order": source_order,
                        "line_order": line_order,
                    }
                )

    messages.sort(
        key=lambda item: (
            parse_timestamp(item["timestamp"]),
            item["source_order"],
            item["line_order"],
        )
    )
    return messages


def render(paths: list[Path], messages: list[dict[str, str]]) -> str:
    lines = [
        "# Agent 协作全程记录",
        "",
        "> 本文件由 Codex 本地会话自动整理生成，保留用户与 Agent 的可见消息原文；",
        "> 系统/开发者提示、内部推理、工具调用参数及原始运行日志未纳入正文。实验命令、结果和证据见项目各题目录。",
        "",
        "## 记录范围",
        "",
        f"- 可见消息：{len(messages)} 条；",
        f"- 用户消息：{sum(m['role'] == 'user' for m in messages)} 条；",
        f"- Agent 消息：{sum(m['role'] == 'assistant' for m in messages)} 条；",
        "- 原始会话文件及 SHA-256：",
        "",
    ]
    for path in paths:
        lines.append(f"  - `{path.name}`：`{sha256(path)}`")

    current_date = None
    for index, message in enumerate(messages, start=1):
        timestamp = parse_timestamp(message["timestamp"])
        date_label = timestamp.astimezone().strftime("%Y-%m-%d")
        time_label = timestamp.astimezone().strftime("%H:%M:%S")
        if date_label != current_date:
            lines.extend(["", f"## {date_label}", ""])
            current_date = date_label
        speaker = "用户" if message["role"] == "user" else "Agent"
        phase = "（过程更新）" if message["phase"] == "commentary" else ""
        lines.extend(
            [
                f"### {index:03d} · {time_label} · {speaker}{phase}",
                "",
                message["text"],
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("-o", "--output", required=True, type=Path)
    args = parser.parse_args()

    paths = [path.resolve() for path in args.inputs]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise SystemExit(f"Missing input files: {', '.join(missing)}")

    messages = collect_messages(paths)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(paths, messages), encoding="utf-8")
    print(f"Wrote {len(messages)} visible messages to {args.output}")


if __name__ == "__main__":
    main()
