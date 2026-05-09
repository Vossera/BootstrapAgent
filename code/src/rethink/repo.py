from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from rethink.schemas import RepoInput


def parse_repo_input(source: str, language_hint: str | None = None) -> RepoInput:
    is_url = source.startswith("http://") or source.startswith("https://") or source.startswith("git@")
    name = _safe_name_from_source(source)
    return RepoInput(source=source, name=name, is_url=is_url, language_hint=language_hint)


def prepare_workspace(repo: RepoInput, destination: Path, *, allow_clone: bool = True, repo_root: Path | None = None) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = resolve_repo_source(repo, repo_root=repo_root, allow_clone=allow_clone)
    if source is None:
        if destination.exists():
            shutil.rmtree(destination)
        subprocess.run(["git", "clone", "--depth", "1", repo.source, str(destination)], check=True)
        return destination
    if source.resolve() == destination.resolve():
        return destination
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".bootstrap",
            ".venv",
            "venv",
            "node_modules",
            "target",
            "dist",
            "__pycache__",
        ),
    )
    return destination


def resolve_repo_source(repo: RepoInput, *, repo_root: Path | None = None, allow_clone: bool = True) -> Path | None:
    if not repo.is_url:
        source = Path(repo.source).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"repo path does not exist: {source}")
        return source

    if repo_root is not None:
        match = find_cached_repo(repo.source, repo_root)
        if match is not None:
            return match
        if not allow_clone:
            raise FileNotFoundError(f"cached repo for {repo.source} was not found under {repo_root}")

    if not allow_clone:
        raise FileNotFoundError(f"repo source is a URL and cloning is disabled: {repo.source}")
    return None


def find_cached_repo(source: str, repo_root: Path) -> Path | None:
    root = repo_root.expanduser()
    candidates = _cached_repo_candidates(source, root)
    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate
    return None


def _cached_repo_candidates(source: str, repo_root: Path) -> list[Path]:
    owner, name = _owner_repo_from_source(source)
    safe_name = _safe_name_from_source(source)
    candidates = [repo_root / name, repo_root / safe_name]
    if owner:
        candidates.extend([repo_root / owner / name, repo_root / f"{owner}__{name}", repo_root / f"{owner}-{name}"])
    return list(dict.fromkeys(candidates))


def _owner_repo_from_source(source: str) -> tuple[str | None, str]:
    text = source.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    if text.startswith("git@") and ":" in text:
        text = text.split(":", 1)[1]
    parts = [part for part in text.split("/") if part]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return None, _safe_name_from_source(source)


def _safe_name_from_source(source: str) -> str:
    text = source.rstrip("/").split("/")[-1]
    if text.endswith(".git"):
        text = text[:-4]
    text = text or "repo"
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    return text.strip("._") or "repo"
