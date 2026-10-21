"""Prepare an allowlisted source snapshot in an existing isolated Git checkout.

No commit, push, credential access, or live-project modification is performed.
The destination's existing LICENSE and Git history are preserved.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT_FILES = ("README.md", "AGENTS.md", ".gitignore", ".env.example",
              "pyproject.toml", "requirements-lock.txt")
SOURCE_DIRS = ("src", "docs", "examples", "scripts", "tools", "tests")
SECRET_PATTERNS = {
    "OpenAI-like key": re.compile(rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}"),
    "Google API key": re.compile(rb"\bAIza[A-Za-z0-9_-]{25,}"),
    "GitHub token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{25,})"),
    "Telegram bot token": re.compile(rb"\b[0-9]{6,12}:[A-Za-z0-9_-]{30,}"),
    "Private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def candidates(root: Path):
    for name in ROOT_FILES:
        yield root / name
    for name in SOURCE_DIRS:
        for folder, dirs, files in os.walk(root / name, followlinks=False):
            dirs[:] = [d for d in dirs if not (
                d == "__pycache__" or d == ".git" or d.startswith(("_tmp", ".pytest"))
            )]
            for filename in files:
                yield Path(folder) / filename


def scan(files: list[Path], base: Path):
    findings = []
    for path in files:
        relative = path.relative_to(base).as_posix()
        if path.is_symlink():
            findings.append((relative, "symlink requires explicit review", 0))
            continue
        data = path.read_bytes()
        for kind, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(data):
                findings.append((relative, kind, data[:match.start()].count(b"\n") + 1))
        if path.suffix.lower() == ".json":
            try:
                payload = json.loads(data)
            except (ValueError, UnicodeError):
                continue

            def inspect(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key.lower() in {"access_token", "refresh_token", "client_secret",
                                           "private_key", "api_key"} and (
                            isinstance(child, str) and len(child) >= 20
                        ):
                            findings.append((relative, "credential-bearing JSON field", 0))
                        inspect(child)
                elif isinstance(value, list):
                    for child in value:
                        inspect(child)

            inspect(payload)
    return findings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    target = args.destination.resolve()
    if target == root or not (target / ".git").is_dir():
        raise ValueError("Destination must be a separate existing Git checkout")
    if not target.is_relative_to(root / "projects" / "runtime"):
        raise ValueError("Publishing checkout must be under projects/runtime")
    shutil.copy2(root / ".gitignore", target / ".gitignore")
    source = list(candidates(root))
    names = [p.relative_to(root).as_posix() for p in source]
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "-z", "--stdin"], cwd=target,
        input="\0".join(names).encode() + b"\0", capture_output=True, check=False,
    )
    if ignored.returncode not in (0, 1):
        raise RuntimeError("Could not evaluate Git ignore rules")
    ignored_names = set(ignored.stdout.decode().strip("\0").split("\0"))
    publish = [p for p in source if p.relative_to(root).as_posix() not in ignored_names]
    findings = scan(publish, root)
    if findings:
        # Report location/type only; never print a matched credential.
        for path, kind, line in findings:
            print(f"REVIEW {path}:{line}: {kind}")
        raise SystemExit("Secret scan needs review; no source snapshot copied")
    for path in publish:
        destination = target / path.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    size = sum(p.stat().st_size for p in publish)
    print(json.dumps({"copied_files": len(publish), "bytes": size,
                      "ignored_files": len(ignored_names - {""}),
                      "secret_findings": 0, "destination": str(target)}, indent=2))


if __name__ == "__main__":
    main()
