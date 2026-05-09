#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_BENCHMARK_ROOT = Path("/artifact/local_data/benchmark")


@dataclass(frozen=True)
class Job:
    row_number: int
    repo: str
    name: str
    language: str | None
    out_dir: Path


@dataclass(frozen=True)
class JobResult:
    job: Job
    returncode: int
    started_at: str
    ended_at: str
    duration_sec: float


def main() -> int:
    parser = argparse.ArgumentParser(description="Run rethink bootstrap for a 1-based inclusive CSV row slice.")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("start", type=int, help="First data row to run, 1-based and excluding the CSV header.")
    parser.add_argument("end", type=int, help="Last data row to run, 1-based and excluding the CSV header.")
    parser.add_argument("-j", "--jobs", type=int, default=1, help="Number of parallel bootstrap processes.")
    parser.add_argument("--out", type=Path, default=Path("runs"), help="Output root; each run writes to OUT/<repo-name>.")
    parser.add_argument("--model", default="deepseek:deepseek-chat")
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument("--allow-fallback", action="store_true")
    parser.add_argument("--warm-repair", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--log-llm-conversations", action="store_true", help="Forward full LLM conversation logging to each rethink bootstrap run.")
    parser.add_argument("--repo-root", action="append", type=Path, default=[], help="Additional local repository root. Can be repeated.")
    parser.add_argument("--benchmark", "--clone-cache", dest="benchmark", type=Path, default=DEFAULT_BENCHMARK_ROOT, help="Local benchmark repository root.")
    parser.add_argument("--allow-clone", action="store_true", help="Allow this script to clone missing CSV URLs into --benchmark.")
    parser.add_argument("--python", default=sys.executable, help="Python executable used to run rethink.cli.")
    args = parser.parse_args()

    if args.start < 1 or args.end < args.start:
        parser.error("start/end must be a valid 1-based inclusive data-row range")
    if args.jobs < 1:
        parser.error("--jobs must be >= 1")

    repo_roots = _repo_roots([args.benchmark, *args.repo_root], args.csv_path)
    jobs = _load_jobs(args.csv_path, args.start, args.end, args.out, repo_roots, benchmark=args.benchmark, allow_clone=args.allow_clone)
    if not jobs:
        print(f"[rethink-slice] no rows selected from {args.csv_path}", file=sys.stderr, flush=True)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    print(
        f"[rethink-slice] running rows {args.start}-{args.end}: {len(jobs)} repos with jobs={args.jobs}",
        file=sys.stderr,
        flush=True,
    )

    failures = 0
    slice_started_monotonic = time.monotonic()
    slice_started_at = datetime.now(timezone.utc)
    results: list[JobResult] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {executor.submit(_run_job, job, args): job for job in jobs}
        for future in as_completed(futures):
            job = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                now = datetime.now(timezone.utc)
                result = JobResult(
                    job=job,
                    returncode=1,
                    started_at=now.isoformat(),
                    ended_at=now.isoformat(),
                    duration_sec=0.0,
                )
                print(f"[rethink-slice] row {job.row_number} {job.name}: failed with {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            results.append(result)
            if result.returncode != 0:
                failures += 1

    slice_ended_at = datetime.now(timezone.utc)
    total_wall_sec = max(time.monotonic() - slice_started_monotonic, 0.0)
    _write_slice_manifest(args, jobs, failures, results, started_at=slice_started_at, ended_at=slice_ended_at, total_wall_sec=total_wall_sec)
    if failures:
        print(
            f"[rethink-slice] completed with {failures}/{len(jobs)} failures in {_format_duration(total_wall_sec)}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(f"[rethink-slice] completed successfully: {len(jobs)} runs in {_format_duration(total_wall_sec)}", file=sys.stderr, flush=True)
    return 0


def _load_jobs(csv_path: Path, start: int, end: int, out_root: Path, repo_roots: list[Path], *, benchmark: Path | None = None, allow_clone: bool) -> list[Job]:
    jobs: list[Job] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row_index, row in enumerate(reader, start=1):
            if row_index < start:
                continue
            if row_index > end:
                break
            repo = (row.get("local_path") or row.get("path") or row.get("repo_path") or row.get("url") or row.get("repo") or row.get("repository") or "").strip()
            if not repo:
                raise ValueError(f"row {row_index} has no url/repo/repository field")
            name = _repo_name(row, repo)
            local_repo = _resolve_local_repo(repo, row, repo_roots)
            if local_repo is None and _is_url(repo):
                if not allow_clone or benchmark is None:
                    roots = ", ".join(str(root) for root in repo_roots) or "(none)"
                    raise FileNotFoundError(f"row {row_index} {name}: local repo for {repo} not found; searched {roots}")
                local_repo = _ensure_cached_clone(repo, row, benchmark)
            language = (row.get("language") or "").strip() or None
            jobs.append(Job(row_number=row_index, repo=str(local_repo or repo), name=name, language=language, out_dir=out_root / name))
    return jobs


def _repo_roots(explicit_roots: list[Path], csv_path: Path) -> list[Path]:
    roots: list[Path] = []
    env_root = os.environ.get("RETHINK_REPO_ROOT")
    if env_root:
        roots.extend(Path(part) for part in env_root.split(os.pathsep) if part)
    roots.extend(explicit_roots)
    cwd = Path.cwd()
    csv_dir = csv_path.resolve().parent
    roots.extend(
        [
            cwd / "repos",
            cwd / "repositories",
            cwd / "local_repos",
            cwd / "launcher_repos",
            cwd / "projects",
            csv_dir / "repos",
            csv_dir / "repositories",
            Path.home() / "repos",
            Path.home() / "github",
        ]
    )
    resolved: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        expanded = root.expanduser().resolve()
        if expanded not in seen:
            resolved.append(expanded)
            seen.add(expanded)
    return resolved


def _resolve_local_repo(repo: str, row: dict[str, str], repo_roots: list[Path]) -> Path | None:
    path = Path(repo).expanduser()
    if not _is_url(repo) and path.exists():
        return path.resolve()
    for root in repo_roots:
        for candidate in _local_repo_candidates(repo, row, root):
            if candidate.exists() and candidate.is_dir():
                return candidate
    return None


def _ensure_cached_clone(repo: str, row: dict[str, str], clone_cache: Path) -> Path:
    target = clone_cache / _repo_name(row, repo)
    if target.exists() and target.is_dir():
        return target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"[rethink-slice] cloning {repo} -> {target}", file=sys.stderr, flush=True)
    subprocess.run(["git", "clone", "--recurse-submodules", "--depth", "1", "--shallow-submodules", repo, str(target)], check=True)
    return target.resolve()


def _local_repo_candidates(repo: str, row: dict[str, str], root: Path) -> list[Path]:
    owner, name = _owner_repo_from_source(repo)
    safe_name = (row.get("safe_name") or "").strip()
    display_name = (row.get("name") or "").strip()
    values = [name, safe_name, display_name, _safe_name(name)]
    candidates = [root / value for value in values if value]
    if owner and name:
        candidates.extend([root / owner / name, root / f"{owner}__{name}", root / f"{owner}-{name}"])
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
    return None, _name_from_repo(source)


def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://") or value.startswith("git@")


def _repo_name(row: dict[str, str], repo: str) -> str:
    raw = (row.get("safe_name") or "").strip() or (row.get("name") or "").strip() or _name_from_repo(repo)
    if not raw:
        raise ValueError(f"cannot determine repository name for {repo}")
    return _safe_name(raw)


def _name_from_repo(repo: str) -> str:
    text = repo.rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    if "/" in text:
        return text.rsplit("/", 1)[1]
    return text


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    safe = safe.strip(".-")
    return safe or "repo"


def _run_job(job: Job, args: argparse.Namespace) -> JobResult:
    command = [
        args.python,
        "-m",
        "rethink.cli",
        "bootstrap",
        "--repo",
        job.repo,
        "--out",
        str(job.out_dir),
        "--model",
        args.model,
    ]
    if args.no_verify:
        command.append("--no-verify")
    if args.allow_fallback:
        command.append("--allow-fallback")
    if args.warm_repair:
        command.append("--warm-repair")
    if args.log_llm_conversations:
        command.append("--log-llm-conversations")
    command.append("--no-clone")
    if job.language:
        command.extend(["--language", job.language])

    job.out_dir.mkdir(parents=True, exist_ok=True)
    log_path = job.out_dir / "bootstrap_process.log"
    started_monotonic = time.monotonic()
    started_at = datetime.now(timezone.utc)
    print(f"[rethink-slice] row {job.row_number} {job.name}: starting -> {job.out_dir} (log: {log_path})", file=sys.stderr, flush=True)
    env = os.environ.copy()
    repo_src = Path(__file__).resolve().parents[1] / "src"
    if repo_src.exists():
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = f"{repo_src}{os.pathsep}{existing}" if existing else str(repo_src)
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ {shlex.join(command)}\n\n")
        log.flush()
        completed = subprocess.run(command, check=False, env=env, stdout=log, stderr=subprocess.STDOUT)
    ended_at = datetime.now(timezone.utc)
    duration_sec = max(time.monotonic() - started_monotonic, 0.0)
    if completed.returncode != 0 and not (job.out_dir / "evaluation_log.json").exists():
        _write_runner_failure_log(job, completed.returncode, command, duration_sec=duration_sec)
    status = "success" if completed.returncode == 0 else f"failed exit={completed.returncode}"
    print(
        f"[rethink-slice] row {job.row_number} {job.name}: {status} in {_format_duration(duration_sec)} (log: {log_path})",
        file=sys.stderr,
        flush=True,
    )
    return JobResult(
        job=job,
        returncode=completed.returncode,
        started_at=started_at.isoformat(),
        ended_at=ended_at.isoformat(),
        duration_sec=duration_sec,
    )


def _write_runner_failure_log(job: Job, returncode: int, command: list[str], *, duration_sec: float = 0.0) -> None:
    payload = {
        "status": "fail",
        "bootstrap_path": str(job.out_dir / "workspace" / "repo" / ".bootstrap"),
        "maturity_reached": "none",
        "failed_stage": "runner",
        "minimal_passed": False,
        "strongest_passed": False,
        "run_probe_passed": None,
        "stage_results": [
            {
                "stage": "runner",
                "status": "fail",
                "command": shlex.join(command),
                "exit_code": returncode,
                "elapsed_sec": 0.0,
                "failure_type": "runner_failure",
            }
        ],
        "token_cost": None,
        "wall_clock_time_sec": duration_sec,
        "command_count": 0,
        "retry_count": 0,
        "stop_reason": "verifier_failed",
        "trace_files": [],
        "agent_output_files": [],
        "metadata": {
            "error": f"bootstrap subprocess exited {returncode} without writing evaluation_log.json",
            "repo": {"source": job.repo, "name": job.name, "language_hint": job.language},
            "row_number": job.row_number,
            "runner_returncode": returncode,
        },
    }
    (job.out_dir / "evaluation_log.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_slice_manifest(
    args: argparse.Namespace,
    jobs: list[Job],
    failures: int,
    results: list[JobResult],
    *,
    started_at: datetime,
    ended_at: datetime,
    total_wall_sec: float,
) -> None:
    result_by_row = {result.job.row_number: result for result in results}
    payload = {
        "created_at": ended_at.isoformat(),
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "total_wall_time_sec": total_wall_sec,
        "total_wall_time": _format_duration(total_wall_sec),
        "csv_path": str(args.csv_path),
        "start": args.start,
        "end": args.end,
        "jobs": args.jobs,
        "model": args.model,
        "warm_repair": args.warm_repair,
        "count": len(jobs),
        "failures": failures,
        "runs": [
            {
                "row_number": job.row_number,
                "name": job.name,
                "repo": job.repo,
                "language": job.language,
                "out_dir": str(job.out_dir),
                "returncode": result_by_row[job.row_number].returncode if job.row_number in result_by_row else None,
                "started_at": result_by_row[job.row_number].started_at if job.row_number in result_by_row else None,
                "ended_at": result_by_row[job.row_number].ended_at if job.row_number in result_by_row else None,
                "duration_sec": result_by_row[job.row_number].duration_sec if job.row_number in result_by_row else None,
                "duration": _format_duration(result_by_row[job.row_number].duration_sec) if job.row_number in result_by_row else None,
            }
            for job in jobs
        ],
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "slice_manifest.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


if __name__ == "__main__":
    raise SystemExit(main())
