#!/usr/bin/env python
"""Generate the synthetic benchmark corpus for everything-mcp.

Layout (deterministic, seeded):

    benchmarks/.data/
        small_scope/                 200 files (single-directory overhead scenario)
        g.../s.../...                ~199 nested directories, depth <= 3
        <30,000 files total>

Mix:
  * extensions: txt, log, json, py, md, bin, dat, img
  * sizes: lognormal (median ~12 KiB) in [1 KiB, 50 MiB]; the text family is
    capped at 256 KiB so content-grep workloads stay honest, large files are
    binary.  Five extra files are > 100 MiB (sparse: header + truncate).
  * mtimes: spread over the last 2 years, ~2.5% of files fall inside the last
    7 days (used by the ``recent_changes`` scenario).
  * ~300 intentional duplicate groups (same byte length + identical content).
  * a fixed ASCII token (``benchmark-token``) planted in ~350 ``.txt`` files
    for the ``content_grep`` scenario.

``benchmarks/dataset_manifest.json`` is committed; the ``.data/`` directory is
gitignored and never committed.

Usage:
    python benchmarks/gen_dataset.py            # create if missing
    python benchmarks/gen_dataset.py --force    # wipe and regenerate
    python benchmarks/gen_dataset.py --out D:\\tmp\\corpus --force
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = BENCH_DIR / ".data"
MANIFEST_PATH = BENCH_DIR / "dataset_manifest.json"

TEXT_EXTS = ("txt", "log", "json", "py", "md")
BINARY_EXTS = ("bin", "dat", "img")
ALL_EXTS = TEXT_EXTS + BINARY_EXTS
EXT_WEIGHTS = {"txt": 22, "log": 18, "json": 15, "py": 12, "md": 10,
               "bin": 10, "dat": 8, "img": 5}

TARGET_FILES = 30_000
TARGET_DIRS = 200          # directories below .data (root excluded)
SMALL_SCOPE_FILES = 200
DUP_GROUPS = 300
BIG_FILES = 5
TOKEN = "benchmark-token"
TOKEN_FILES = 350
RECENT_FRACTION = 40       # 1 in 40 files gets an mtime inside the last 7 days
MIN_SIZE = 1024
MAX_SIZE = 50 * 1024 * 1024
TEXT_MAX_SIZE = 256 * 1024
RECENT_WINDOW_SECONDS = 7 * 24 * 3600
TWO_YEARS_SECONDS = 730 * 24 * 3600


def _make_dirs(root: Path, rng: random.Random) -> list[Path]:
    """Create ``TARGET_DIRS`` nested directories (depth <= 3), return them.

    Deterministic BFS layout: 1 ``small_scope`` + 20 top-level + 60 second-level
    + 119 third-level dirs = 200.  (``rng`` is kept for signature stability.)
    """
    dirs: list[Path] = []
    small = root / "small_scope"
    small.mkdir(parents=True, exist_ok=True)
    dirs.append(small)

    tops: list[Path] = []
    for i in range(20):
        child = root / f"g{i:03d}"
        child.mkdir(parents=True, exist_ok=True)
        tops.append(child)
        dirs.append(child)

    seconds: list[Path] = []
    for i, parent in enumerate(tops):
        for j in range(3):
            child = parent / f"s{i:02d}_{j}"
            child.mkdir(parents=True, exist_ok=True)
            seconds.append(child)
            dirs.append(child)

    for i, parent in enumerate(seconds):
        for j in range(2 if i < 59 else 1):
            child = parent / f"t{i:02d}_{j}"
            child.mkdir(parents=True, exist_ok=True)
            dirs.append(child)
    return dirs


def _draw_sizes(count: int, rng: random.Random) -> list[int]:
    """Draw ``count`` lognormal sizes in [1 KiB, 50 MiB].

    Uniqueness is *not* required for normal files: duplicates are the deliberate
    ``dupe-*`` groups below, incidental equal-size files are harmless (both the
    native and the index side group by exact byte length).
    """
    sizes: list[int] = []
    while len(sizes) < count:
        value = int(rng.lognormvariate(math.log(12 * 1024), 1.15))
        if value < MIN_SIZE or value > MAX_SIZE:
            continue
        sizes.append(value)
    return sizes


def _text_size(size: int, rng: random.Random) -> int:
    """Clamp text-family sizes to 256 KiB (keeps content-grep workloads fair)."""
    if size <= TEXT_MAX_SIZE:
        return size
    return rng.randint(MIN_SIZE, TEXT_MAX_SIZE)


def _text_content(size: int, token: bool, rng: random.Random) -> bytes:
    head = f"{TOKEN} line\n" if token else ""
    line = (
        "the quick brown fox jumps over the lazy dog 0123456789 "
        "everything index benchmark dataset line\n"
    )
    body = head + (line * (size // len(line) + 2))
    return body[:size].encode("ascii", errors="replace")


def _binary_content(size: int, rng: random.Random) -> bytes:
    header = bytes(rng.getrandbits(8) for _ in range(min(512, size)))
    return header


def _write_file(path: Path, size: int, ext: str, token: bool, rng: random.Random,
                mtime: float) -> None:
    if ext in TEXT_EXTS:
        data = _text_content(size, token, rng)
        with open(path, "wb") as handle:
            handle.write(data)
    else:
        data = _binary_content(size, rng)
        with open(path, "wb") as handle:
            handle.write(data)
            if size > len(data):
                # NTFS: extend the file without materialising the whole payload.
                handle.truncate(size)
    try:
        os.utime(path, (mtime, mtime))
    except OSError:
        pass


def generate(out: Path = DEFAULT_OUT, *, force: bool = False, seed: int = 20261018) -> dict:
    out = Path(out)
    manifest_path = MANIFEST_PATH
    if out.exists() and not force:
        if (out / "small_scope").is_dir():
            print(f"[gen_dataset] {out} already exists (use --force to regenerate)")
            return json.loads(manifest_path.read_text(encoding="utf-8"))
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    rng = random.Random(seed)
    now = time.time()

    dirs = _make_dirs(out, rng)
    if len(dirs) != TARGET_DIRS:
        raise SystemExit(f"expected {TARGET_DIRS} dirs, got {len(dirs)}")

    # ---- plan file slots -------------------------------------------------
    # 1) duplicate groups (unique byte sizes, identical content per group)
    dup_sizes: list[int] = []
    dup_plan: list[tuple[int, int, str]] = []  # (size, copies, ext)
    used_sizes: set[int] = set()
    for group in range(DUP_GROUPS):
        size = 4096 + group * 997 + rng.randint(0, 500)
        while size in used_sizes:
            size += 1
        used_sizes.add(size)
        copies = rng.choice((2, 2, 3, 3, 4))
        ext = rng.choice(("txt", "log", "json", "dat"))
        dup_sizes.append(size)
        dup_plan.append((size, copies, ext))
    dup_files = sum(copies for _, copies, _ in dup_plan)

    # 2) five > 100 MiB files
    big_sizes = [120 * 1024 * 1024 + i * (17 * 1024 * 1024) for i in range(BIG_FILES)]
    for size in big_sizes:
        while size in used_sizes:
            size += 1
        used_sizes.add(size)

    normal_count = TARGET_FILES - SMALL_SCOPE_FILES - dup_files - BIG_FILES
    if normal_count < 0:
        raise SystemExit("duplicate plan exceeds target file count")

    normal_sizes = _draw_sizes(normal_count + SMALL_SCOPE_FILES, rng)
    rng.shuffle(normal_sizes)
    small_sizes = normal_sizes[:SMALL_SCOPE_FILES]
    rest_sizes = normal_sizes[SMALL_SCOPE_FILES:]

    # extension per normal file (weighted, deterministic)
    ext_pool = [ext for ext, weight in EXT_WEIGHTS.items() for _ in range(weight)]
    normal_exts = [rng.choice(ext_pool) for _ in range(len(normal_sizes))]
    small_exts = normal_exts[:SMALL_SCOPE_FILES]
    rest_exts = normal_exts[SMALL_SCOPE_FILES:]

    # token planting: spread the 350 token files over text extensions,
    # deterministic indices derived from the global file index.
    token_slots: set[int] = set()
    token_rng = random.Random(seed ^ 0x5EED)
    candidates = [i for i, ext in enumerate(rest_exts) if ext in ("txt", "log")]
    token_rng.shuffle(candidates)
    token_slots = set(candidates[:TOKEN_FILES])

    # ---- write files -----------------------------------------------------
    readable_dirs = dirs[1:]
    ext_counts: dict[str, int] = {ext: 0 for ext in ALL_EXTS}
    size_buckets = {
        "1KB-10KB": 0,
        "10KB-100KB": 0,
        "100KB-1MB": 0,
        "1MB-10MB": 0,
        "10MB-50MB": 0,
        ">=100MB": 0,
    }
    total_bytes = 0

    def bucket(size: int) -> str:
        if size >= 100 * 1024 * 1024:
            return ">=100MB"
        if size >= 10 * 1024 * 1024:
            return "10MB-50MB"
        if size >= 1024 * 1024:
            return "1MB-10MB"
        if size >= 100 * 1024:
            return "100KB-1MB"
        if size >= 10 * 1024:
            return "10KB-100KB"
        return "1KB-10KB"

    def _record(size: int, ext: str) -> None:
        nonlocal total_bytes
        ext_counts[ext] += 1
        size_buckets[bucket(size)] += 1
        total_bytes += size

    for idx, (size, ext) in enumerate(zip(small_sizes, small_exts)):
        ext = ext if ext in TEXT_EXTS else "txt"
        target_size = _text_size(size, rng)
        name = f"item-{idx:05d}-{rng.getrandbits(28):07x}.{ext}"
        path = dirs[0] / name
        mtime = _pick_mtime(rng, now, idx)
        _write_file(path, target_size, ext, False, rng, mtime)
        _record(target_size, ext)

    normal_written = 0
    for idx, (size, ext) in enumerate(zip(rest_sizes, rest_exts)):
        global_idx = SMALL_SCOPE_FILES + idx
        if ext in TEXT_EXTS:
            size = _text_size(size, rng)
        name = f"item-{global_idx:05d}-{rng.getrandbits(28):07x}.{ext}"
        folder = readable_dirs[idx % len(readable_dirs)]
        path = folder / name
        mtime = _pick_mtime(rng, now, global_idx)
        _write_file(path, size, ext, idx in token_slots, rng, mtime)
        _record(size, ext)
        normal_written += 1

    for plan_idx, (size, copies, ext) in enumerate(dup_plan):
        payload = _text_content(size, False, rng) if ext in TEXT_EXTS else _binary_content(size, rng)
        for copy in range(copies):
            name = f"dupe-{plan_idx:04d}-{copy}-{rng.getrandbits(24):06x}.{ext}"
            folder = readable_dirs[(plan_idx * 7 + copy) % len(readable_dirs)]
            path = folder / name
            with open(path, "wb") as handle:
                handle.write(payload)
                if size > len(payload):
                    handle.truncate(size)
            mtime = _pick_mtime(rng, now, 900_000 + plan_idx)
            os.utime(path, (mtime, mtime))
            _record(size, ext)

    for big_idx, size in enumerate(big_sizes):
        name = f"huge-{big_idx:02d}-{rng.getrandbits(24):06x}.bin"
        folder = readable_dirs[big_idx % len(readable_dirs)]
        path = folder / name
        _write_file(path, size, "bin", False, rng, _pick_mtime(rng, now, 950_000 + big_idx))
        _record(size, "bin")

    # ---- verify + manifest ----------------------------------------------
    written = sum(1 for _ in out.rglob("*") if _.is_file())
    if written != TARGET_FILES:
        raise SystemExit(f"expected {TARGET_FILES} files, wrote {written}")
    dir_count = sum(1 for _ in out.rglob("*") if _.is_dir())
    if dir_count != TARGET_DIRS:
        raise SystemExit(f"expected {TARGET_DIRS} dirs, wrote {dir_count}")

    recent_count = sum(
        1
        for path in out.rglob("*")
        if path.is_file() and (now - path.stat().st_mtime) <= RECENT_WINDOW_SECONDS
    )
    token_count = sum(
        1
        for path in out.rglob("*.txt")
        if path.is_file() and TOKEN.encode() in path.read_bytes()
    )
    dup_total = sum(copies for _, copies, _ in dup_plan)

    manifest = {
        "seed": seed,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": ".data",
        "total_files": written,
        "total_dirs": dir_count,
        "max_depth": 3,
        "total_bytes": total_bytes,
        "extensions": list(ALL_EXTS),
        "counts_per_ext": ext_counts,
        "size_buckets": size_buckets,
        "duplicate_groups": DUP_GROUPS,
        "duplicate_files": dup_total,
        "large_files_100mb": BIG_FILES,
        "recent_files_7d": recent_count,
        "content_token": TOKEN,
        "content_token_files": token_count,
        "filename_pattern": "item-<5 digits>-<7 hex>.<ext> / dupe-... / huge-...",
        "notes": (
            "Sizes are lognormal (median ~12 KiB) within [1 KiB, 50 MiB]; the text "
            "family is capped at 256 KiB so the content-grep scenario stays fair. "
            "Files > 100 MiB are written as a header plus truncate (sparse-ish)."
        ),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    elapsed = time.perf_counter() - started
    print(
        f"[gen_dataset] {written} files / {dir_count} dirs, {total_bytes / (1024**3):.2f} GiB "
        f"logical, {TOKEN_FILES} token files, {dup_total} duplicate files "
        f"in {elapsed:.1f}s -> {manifest_path}"
    )
    return manifest


def _pick_mtime(rng: random.Random, now: float, index: int) -> float:
    if index % RECENT_FRACTION == 0:
        # inside the last 7 days, away from the exact boundary
        offset = rng.uniform(0.2 * 24 * 3600, 5.0 * 24 * 3600)
        return now - offset
    offset = rng.uniform(7.1 * 24 * 3600, TWO_YEARS_SECONDS)
    return now - offset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output directory (.data)")
    parser.add_argument("--force", action="store_true", help="wipe and regenerate")
    parser.add_argument("--seed", type=int, default=20261018)
    args = parser.parse_args(argv)
    generate(Path(args.out), force=args.force, seed=args.seed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
