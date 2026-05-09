#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


DEFAULT_BENCHMARK_ROOT = Path("/artifact/local_data/benchmark")


@dataclass(frozen=True)
class Repo:
    row_number: int
    name: str
    url: str
    target: Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Clone benchmark repositories listed in a CSV file.")
    parser.add_argument("csv_path", type=Path, help="CSV containing url/repo/repository plus optional name/safe_name columns.")
    parser.add_argument("--out", type=Path, default=DEFAULT_BENCHMARK_ROOT, help="Directory where repositories are cloned.")
    parser.add_argument("--depth", type=int, default=1, help="Git clone depth. Use 0 for a full clone.")
    parser.add_argument("--update", action="store_true", help="Fetch and update submodules for repositories that already exist.")
    parser.add_argument("--force", action="store_true", help="Delete existing target directories before cloning.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned targets without cloning.")
    args = parser.parse_args()

    repos = _load_repos(args.csv_path, args.out)
    args.out.mkdir(parents=True, exist_ok=True)
    for repo in repos:
        if args.dry_run:
            print(f"{repo.row_number}: {repo.url} -> {repo.target}")
            continue
        _clone_or_update(repo, depth=args.depth, update=args.update, force=args.force)
    return 0


def _load_repos(csv_path: Path, out_root: Path) -> list[Repo]:
    repos: list[Repo] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row_number, row in enumerate(reader, start=1):
            url = _first_nonempty(row, "url", "repo", "repository", "clone_url", "html_url")
            if not url:
                raise ValueError(f"row {row_number} has no url/repo/repository field")
            name = _safe_name(_first_nonempty(row, "safe_name", "name", "full_name") or _name_from_url(url))
            repos.append(Repo(row_number=row_number, name=name, url=url, target=out_root / name))
    return repos


def _clone_or_update(repo: Repo, *, depth: int, update: bool, force: bool) -> None:
    if repo.target.exists():
        if force:
            shutil.rmtree(repo.target)
        elif update:
            subprocess.run(["git", "-C", str(repo.target), "fetch", "--all", "--prune"], check=True)
            submodule_command = ["git", "-C", str(repo.target), "submodule", "update", "--init", "--recursive"]
            if depth > 0:
                submodule_command.extend(["--depth", str(depth)])
            subprocess.run(submodule_command, check=True)
            return
        else:
            return

    repo.target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(_clone_command(repo.url, repo.target, depth=depth), check=True)


def _clone_command(url: str, target: Path, *, depth: int) -> list[str]:
    command = ["git", "clone", "--recurse-submodules"]
    if depth > 0:
        command.extend(["--depth", str(depth), "--shallow-submodules"])
    command.extend([url, str(target)])
    return command


def _first_nonempty(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = (row.get(key) or "").strip()
        if value:
            return value
    return ""


def _name_from_url(url: str) -> str:
    text = url.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    if "/" in text:
        return text.rsplit("/", 1)[1]
    return text


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    safe = safe.strip(".-")
    return safe or "repo"


if __name__ == "__main__":
    raise SystemExit(main())
