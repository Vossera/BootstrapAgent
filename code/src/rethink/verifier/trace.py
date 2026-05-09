from __future__ import annotations

import re

from rethink.schemas import CommandTrace, FailureSignature, Maturity


def summarize_output(text: str, limit: int = 4000) -> str:
    text = text.replace("\r\n", "\n")
    lines = [line.rstrip() for line in text.splitlines()]
    relevant = [line for line in lines if line.strip()]
    summary = "\n".join(relevant[-80:])
    return summary[-limit:]


def failure_signature(command: str, cwd: str, exit_code: int | None, stdout: str, stderr: str, timeout: bool) -> FailureSignature | None:
    if exit_code == 0 and not timeout:
        return None
    combined = "\n".join([stderr, stdout]).strip()
    snippet = _normalize_error(combined)
    return FailureSignature(
        command=command,
        cwd=cwd,
        exit_code=exit_code,
        normalized_error_snippet=snippet,
        failure_type=_failure_type(snippet, timeout),
    )


def build_trace(
    command: str,
    cwd: str,
    exit_code: int | None,
    elapsed_sec: float,
    stdout: str,
    stderr: str,
    timeout: bool,
    *,
    stage: str | None = None,
    maturity_target: Maturity | None = None,
) -> CommandTrace:
    stdout_summary = summarize_output(stdout)
    stderr_summary = summarize_output(stderr)
    return CommandTrace(
        command=command,
        cwd=cwd,
        exit_code=exit_code,
        elapsed_sec=elapsed_sec,
        stage=stage,
        maturity_target=maturity_target,
        stdout_summary=stdout_summary,
        stderr_summary=stderr_summary,
        timeout=timeout,
        failure_signature=failure_signature(command, cwd, exit_code, stdout_summary, stderr_summary, timeout),
    )


def _normalize_error(text: str) -> str:
    text = re.sub(r"/[^\s:]+", "<path>", text)
    text = re.sub(r"\b\d+\.\d+(?:\.\d+)?\b", "<version>", text)
    text = re.sub(r"\b\d+\b", "<num>", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-12:])[:1200]


def _failure_type(snippet: str, timeout: bool) -> str:
    lowered = snippet.lower()
    if timeout:
        return "timeout"
    if "command not found" in lowered or "not recognized" in lowered:
        return "missing_command"
    if "no module named" in lowered or "cannot find module" in lowered:
        return "missing_dependency"
    if "permission denied" in lowered:
        return "permission_denied"
    if "could not resolve" in lowered or "temporary failure" in lowered or "network" in lowered:
        return "network"
    if _looks_like_test_failure(lowered):
        return "test_failure"
    return "unknown"


def _looks_like_test_failure(lowered: str) -> bool:
    return any(
        marker in lowered
        for marker in [
            "there are test failures",
            "<<< failure!",
            "[error] failures:",
            "failed tests:",
            "short test summary info",
            "test failed",
            "tests failed",
            "failure! -- in ",
            "failures!!!",
            "go test",
            "cargo test",
        ]
    )
