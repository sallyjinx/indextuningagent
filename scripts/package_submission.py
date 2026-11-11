#!/usr/bin/env python3
"""Package the clean repository allowlist without local credentials or runtimes."""

from pathlib import Path
import hashlib
import json
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
DIRECTORIES = ("src", "sql", "prompts", "scripts", "manifests", "compatibility", "docs", "results")
ROOT_FILES = ("README.md", "pyproject.toml", "requirements.lock", "compose.yaml", ".env.example", ".gitignore")


def included(path: Path) -> bool:
    return (
        path.is_file()
        and "__pycache__" not in path.parts
        and ".matplotlib" not in path.parts
        and path.suffix != ".pyc"
        and not path.name.startswith(".env.")
    )


files: list[Path] = []
for directory in DIRECTORIES:
    base = ROOT / directory
    if base.exists():
        files.extend(path for path in base.rglob("*") if included(path))
files.extend(ROOT / name for name in ROOT_FILES if (ROOT / name).is_file())
files = sorted(set(files))

DIST.mkdir(parents=True, exist_ok=True)
manifest = {
    str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in files
}
manifest_path = DIST / "submission-files.json"
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

archive = DIST / "submission.zip"
with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
    for path in files:
        output.write(path, "PostgreSQL-Autonomous-Tuning-Agent/" + str(path.relative_to(ROOT)))
    output.write(manifest_path, "PostgreSQL-Autonomous-Tuning-Agent/submission-files.json")

print(f"{archive}: {len(files) + 1} files, {archive.stat().st_size} bytes")
