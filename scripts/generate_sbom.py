"""Generate a lightweight SBOM-like manifest for repository artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate SBOM manifest.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--output", default="tests/results/sbom_manifest.json")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    include_ext = {".py", ".yaml", ".yml", ".json", ".toml", ".md"}
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if ".git" in path.parts:
            continue
        if path.suffix.lower() not in include_ext:
            continue
        rel = str(path.relative_to(root))
        files.append(
            {
                "path": rel,
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
        )

    payload = {
        "format": "acaf-sbom-v1",
        "root": str(root),
        "python": platform.python_version(),
        "files": files,
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"sbom written: {out}")
    print(f"files: {len(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
