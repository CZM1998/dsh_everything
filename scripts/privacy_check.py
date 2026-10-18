#!/usr/bin/env python
"""Pre-publish privacy check for this repository.

Scans every file that would be committed and reports machine-specific or
personal data that must not appear in a public repository:

  * real Windows user profile directories (``C:\\Users\\<name>``) that are not
    an obvious placeholder;
  * e-mail addresses other than the documentation placeholders;
  * mainland-China mobile numbers;
  * any term listed in an optional ``.privacy-blocklist`` file (one term per
    line, git-ignored) or in the ``PRIVACY_TERMS`` environment variable
    (comma separated).

Usage::

    python scripts/privacy_check.py            # scan files known to git
    python scripts/privacy_check.py --all      # scan the whole working tree

Exits with status 1 when a finding is reported, 0 otherwise.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "dev",
    ".data",
}
SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".pdf",
    ".zip",
    ".7z",
    ".db",
    ".exe",
    ".dll",
    ".pyc",
    ".woff",
    ".woff2",
}

# `C:\Users\<name>` — anything that is not clearly a placeholder.
USER_DIR = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+([^\\/\s\"'<>%$`]+)", re.IGNORECASE)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CN_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

EMAIL_ALLOW = {"user@example.com", "you@example.com", "me@example.com", "noreply@example.com"}


def git_files() -> list[Path]:
    """Files git considers part of the repository (tracked + untracked, no ignored)."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return walk_files()
    return [ROOT / p for p in out.decode("utf-8", "replace").split("\0") if p]


def walk_files() -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            files.append(path)
    return files


def blocklist_terms() -> list[str]:
    terms: list[str] = []
    path = ROOT / ".privacy-blocklist"
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                terms.append(line)
    env = os.environ.get("PRIVACY_TERMS", "")
    terms.extend(t.strip() for t in env.split(",") if t.strip())
    return terms


def scan(path: Path, terms: list[str]) -> list[tuple[int, str, str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    findings: list[tuple[int, str, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in USER_DIR.finditer(line):
            name = match.group(1)
            if name.lower() in {"public", "default", "username", "user", "your-name", "name"}:
                continue
            findings.append((number, "user-profile", match.group(0)))
        for match in EMAIL.finditer(line):
            if match.group(0).lower() in EMAIL_ALLOW:
                continue
            findings.append((number, "email", match.group(0)))
        for match in CN_MOBILE.finditer(line):
            findings.append((number, "phone", match.group(0)))
        lowered = line.lower()
        for term in terms:
            if term.lower() in lowered:
                findings.append((number, "blocklist", term))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="scan the whole working tree")
    parser.add_argument("--quiet", action="store_true", help="only print findings")
    args = parser.parse_args()

    files = walk_files() if args.all else git_files()
    terms = blocklist_terms()
    total = 0
    for path in sorted(files):
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        for number, kind, text in scan(path, terms):
            total += 1
            rel = path.relative_to(ROOT)
            print(f"{rel}:{number}: [{kind}] {text}")

    if not args.quiet:
        print()
        print(f"scanned {len(files)} file(s), {total} finding(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
