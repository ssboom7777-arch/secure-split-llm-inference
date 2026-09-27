from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    root = args.destination
    expected = json.loads(args.manifest.read_text(encoding="utf-8"))
    required = set(expected["files"])
    missing = sorted(required - {path.name for path in root.iterdir() if path.is_file()})
    if missing:
        raise SystemExit(f"Missing Qwen3 files: {missing}")
    index = json.loads((root / "model.safetensors.index.json").read_text(encoding="utf-8"))
    referenced_shards = set(index["weight_map"].values())
    expected_shards = {"model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"}
    if referenced_shards != expected_shards:
        raise SystemExit(f"Unexpected shard references: {sorted(referenced_shards)}")
    for name, metadata in expected["files"].items():
        path = root / name
        if path.stat().st_size != metadata["bytes"]:
            raise SystemExit(f"Size mismatch for {name}: {path.stat().st_size}")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != metadata["sha256"]:
            raise SystemExit(f"SHA-256 mismatch for {name}: {digest.hexdigest()}")
    print(f"Qwen3 snapshot ready: {root}")
    print(f"Revision: {expected['revision']}")
    print(f"Verified files: {len(expected['files'])}")


if __name__ == "__main__":
    main()
