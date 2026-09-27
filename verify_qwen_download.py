from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("revision")
    args = parser.parse_args()
    root = args.destination
    required = {
        "LICENSE", "README.md", "config.json", "generation_config.json",
        "tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt",
        "model.safetensors.index.json", "model-00001-of-00002.safetensors",
        "model-00002-of-00002.safetensors",
    }
    missing = sorted(required - {path.name for path in root.iterdir() if path.is_file()})
    if missing:
        raise SystemExit(f"Missing Qwen3 files: {missing}")
    index = json.loads((root / "model.safetensors.index.json").read_text(encoding="utf-8"))
    referenced_shards = set(index["weight_map"].values())
    expected_shards = {"model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"}
    if referenced_shards != expected_shards:
        raise SystemExit(f"Unexpected shard references: {sorted(referenced_shards)}")
    manifest = {"repo_id": "Qwen/Qwen3-1.7B", "revision": args.revision, "files": {}}
    for path in sorted(p for p in root.iterdir() if p.is_file() and p.name != "download-manifest.json"):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        manifest["files"][path.name] = {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}
    (root / "download-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Qwen3 snapshot ready: {root}")
    print(f"Revision: {manifest['revision']}")
    print(f"Files: {len(manifest['files'])}")


if __name__ == "__main__":
    main()
