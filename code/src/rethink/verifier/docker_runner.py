from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from rethink.config import RuntimeConfig
from rethink.schemas import CommandTrace, Maturity, StopReason
from rethink.verifier.trace import build_trace


class DockerUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandStage:
    name: str
    command: str
    timeout_sec: int
    log_path: Path | None = None
    cwd: str = "."
    maturity_target: Maturity | None = None


class DockerRunner:
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config

    def available(self) -> bool:
        if shutil.which("docker") is None:
            return False
        result = subprocess.run(["docker", "version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return result.returncode == 0

    def _network_args(self) -> list[str]:
        return _network_args_from_config(self.config)

    def run_script(self, repo_dir: Path, script: str, timeout_sec: int, log_path: Path | None = None) -> CommandTrace:
        if not self.available():
            raise DockerUnavailable("docker is not available")
        command = f"bash .bootstrap/{script}"
        docker_command = self._base_docker_command(repo_dir) + ["bash", "-lc", command]
        start = time.monotonic()
        if log_path is not None:
            return self.run_script_sequence(repo_dir, [(script, timeout_sec, log_path)])[0]
        try:
            completed = subprocess.run(
                docker_command,
                text=True,
                capture_output=True,
                timeout=timeout_sec,
                check=False,
            )
            elapsed = time.monotonic() - start
            return build_trace(
                command=command,
                cwd=self.config.workspace_container_path,
                exit_code=completed.returncode,
                elapsed_sec=elapsed,
                stdout=completed.stdout,
                stderr=completed.stderr,
                timeout=False,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - start
            return build_trace(
                command=command,
                cwd=self.config.workspace_container_path,
                exit_code=None,
                elapsed_sec=elapsed,
                stdout=exc.stdout or "",
                stderr=exc.stderr or f"command timed out after {timeout_sec}s",
                timeout=True,
            )

    def run_script_sequence(self, repo_dir: Path, scripts: list[tuple[str, int, Path | None]]) -> list[CommandTrace]:
        stages = [
            CommandStage(name=script.removesuffix(".sh"), command=f"bash .bootstrap/{script}", timeout_sec=timeout, log_path=log_path)
            for script, timeout, log_path in scripts
        ]
        return self.run_command_sequence(repo_dir, stages)

    def run_command_sequence(self, repo_dir: Path, stages: list[CommandStage]) -> list[CommandTrace]:
        if not self.available():
            raise DockerUnavailable("docker is not available")
        if not stages:
            return []
        with tempfile.TemporaryDirectory() as tmp:
            default_log_dir = Path(tmp)
            log_paths = [stage.log_path or (default_log_dir / f"{stage.name}.log") for stage in stages]
            for log_path in log_paths:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("", encoding="utf-8")
            log_mount = log_paths[0].parent.resolve()
            script = _sequence_script(stages, log_paths, self.config.workspace_container_path)
            docker_command = self._base_docker_command(repo_dir, log_mount) + ["bash", "-lc", script]
            start = time.monotonic()
            docker_exit_code: int | None = 0
            docker_output = ""
            docker_timeout = False
            try:
                completed = subprocess.run(
                    docker_command,
                    text=True,
                    capture_output=True,
                    timeout=sum(stage.timeout_sec for stage in stages) + 30,
                    check=False,
                )
                docker_exit_code = completed.returncode
                docker_output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
            except subprocess.TimeoutExpired as exc:
                docker_exit_code = None
                docker_timeout = True
                docker_output = "\n".join(str(part) for part in [exc.stdout or "", exc.stderr or ""] if part)
            elapsed_total = max(time.monotonic() - start, 0.001)
            traces = _read_sequence_traces(log_paths, stages, self.config.workspace_container_path, elapsed_total)
            if docker_timeout or docker_exit_code not in {0, None}:
                traces.append(
                    build_trace(
                        command="docker run verifier sequence",
                        cwd=self.config.workspace_container_path,
                        exit_code=docker_exit_code,
                        elapsed_sec=elapsed_total,
                        stdout=docker_output,
                        stderr="docker verifier sequence failed" if not docker_timeout else "docker verifier sequence timed out",
                        timeout=docker_timeout,
                        stage="verifier_infrastructure",
                    )
                )
            if len(traces) < len(stages) and not any(trace.stage == "verifier_infrastructure" for trace in traces):
                missing = stages[len(traces)]
                log_path = log_paths[len(traces)]
                traces.append(
                    build_trace(
                        command=missing.command,
                        cwd=_container_cwd(self.config.workspace_container_path, missing.cwd),
                        exit_code=125,
                        elapsed_sec=elapsed_total,
                        stdout=log_path.read_text(encoding="utf-8") if log_path.exists() else "",
                        stderr=f"missing verifier end marker for stage {missing.name}",
                        timeout=False,
                        stage=missing.name,
                        maturity_target=missing.maturity_target,
                    )
                )
            return traces

    def _base_docker_command(self, repo_dir: Path, log_mount: Path | None = None) -> list[str]:
        command = [
            "docker",
            "run",
            "--rm",
            *self._network_args(),
            *_proxy_env_args(),
            "-v",
            f"{repo_dir.resolve()}:{self.config.workspace_container_path}",
        ]
        if log_mount is not None:
            command.extend(["-v", f"{log_mount}:/workspace/rethink-logs"])
        command.extend(["-w", self.config.workspace_container_path, self.config.docker_image])
        return command


class WarmDockerRunner:
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.container_name = f"rethink-repair-{uuid.uuid4().hex[:12]}"
        self.repo_dir: Path | None = None
        self.log_mount: Path | None = None
        self.started = False
        self._default_log_dir: tempfile.TemporaryDirectory[str] | None = None

    def available(self) -> bool:
        return DockerRunner(self.config).available()

    def close(self) -> None:
        if not self.started:
            return
        subprocess.run(["docker", "rm", "-f", self.container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        self.started = False
        if self._default_log_dir is not None:
            self._default_log_dir.cleanup()
            self._default_log_dir = None

    def run_script_sequence(self, repo_dir: Path, scripts: list[tuple[str, int, Path | None]]) -> list[CommandTrace]:
        stages = [
            CommandStage(name=script.removesuffix(".sh"), command=f"bash .bootstrap/{script}", timeout_sec=timeout, log_path=log_path)
            for script, timeout, log_path in scripts
        ]
        return self.run_command_sequence(repo_dir, stages)

    def run_command_sequence(self, repo_dir: Path, stages: list[CommandStage]) -> list[CommandTrace]:
        if not self.available():
            raise DockerUnavailable("docker is not available")
        if not stages:
            return []
        default_log_dir = self._get_default_log_dir()
        log_paths = [stage.log_path or (default_log_dir / f"{stage.name}.log") for stage in stages]
        for log_path in log_paths:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8")
        self._ensure_started(repo_dir, log_paths[0].parent.resolve())
        script = _sequence_script(stages, log_paths, self.config.workspace_container_path)
        start = time.monotonic()
        docker_exit_code: int | None = 0
        docker_output = ""
        docker_timeout = False
        try:
            completed = subprocess.run(
                ["docker", "exec", self.container_name, "bash", "-lc", script],
                text=True,
                capture_output=True,
                timeout=sum(stage.timeout_sec for stage in stages) + 30,
                check=False,
            )
            docker_exit_code = completed.returncode
            docker_output = "\n".join(part for part in [completed.stdout, completed.stderr] if part)
        except subprocess.TimeoutExpired as exc:
            docker_exit_code = None
            docker_timeout = True
            docker_output = "\n".join(str(part) for part in [exc.stdout or "", exc.stderr or ""] if part)
        elapsed_total = max(time.monotonic() - start, 0.001)
        traces = _read_sequence_traces(log_paths, stages, self.config.workspace_container_path, elapsed_total)
        if docker_timeout or docker_exit_code not in {0, None}:
            traces.append(
                build_trace(
                    command="docker exec verifier sequence",
                    cwd=self.config.workspace_container_path,
                    exit_code=docker_exit_code,
                    elapsed_sec=elapsed_total,
                    stdout=docker_output,
                    stderr="docker verifier sequence failed" if not docker_timeout else "docker verifier sequence timed out",
                    timeout=docker_timeout,
                    stage="verifier_infrastructure",
                )
            )
        if len(traces) < len(stages) and not any(trace.stage == "verifier_infrastructure" for trace in traces):
            missing = stages[len(traces)]
            log_path = log_paths[len(traces)]
            traces.append(
                build_trace(
                    command=missing.command,
                    cwd=_container_cwd(self.config.workspace_container_path, missing.cwd),
                    exit_code=125,
                    elapsed_sec=elapsed_total,
                    stdout=log_path.read_text(encoding="utf-8") if log_path.exists() else "",
                    stderr=f"missing verifier end marker for stage {missing.name}",
                    timeout=False,
                    stage=missing.name,
                    maturity_target=missing.maturity_target,
                )
            )
        return traces

    def _get_default_log_dir(self) -> Path:
        if self._default_log_dir is None:
            self._default_log_dir = tempfile.TemporaryDirectory()
        return Path(self._default_log_dir.name)

    def _ensure_started(self, repo_dir: Path, log_mount: Path) -> None:
        repo_dir = repo_dir.resolve()
        if self.started:
            if self.repo_dir != repo_dir or self.log_mount != log_mount:
                raise RuntimeError("warm Docker runner cannot change repo or log mount after startup")
            return
        self.repo_dir = repo_dir
        self.log_mount = log_mount
        command = [
            "docker",
            "run",
            "-d",
            *DockerRunner(self.config)._network_args(),
            "--name",
            self.container_name,
            *_proxy_env_args(),
            "-v",
            f"{repo_dir}:{self.config.workspace_container_path}",
            "-v",
            f"{log_mount}:/workspace/rethink-logs",
            "-w",
            self.config.workspace_container_path,
            self.config.docker_image,
            "sleep",
            "infinity",
        ]
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            raise DockerUnavailable(completed.stderr or completed.stdout or "failed to start warm Docker container")
        self.started = True

    def __enter__(self) -> "WarmDockerRunner":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _network_args_from_config(config: RuntimeConfig) -> list[str]:
    if not config.docker_network:
        return []
    return ["--network", config.docker_network]


def _proxy_env_args() -> list[str]:
    args: list[str] = []
    for name in [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ]:
        args.extend(["--env", name])
    return args


def _sequence_script(stages: list[CommandStage], log_paths: list[Path], workspace_container_path: str) -> str:
    cleanup = f"chown -R {os.getuid()}:{os.getgid()} {shlex_quote(workspace_container_path)} >/dev/null 2>&1 || true"
    lines = ["set +e"]
    for index, (stage, _) in enumerate(zip(stages, log_paths)):
        log_name = log_paths[index].name
        cwd = _container_cwd(workspace_container_path, stage.cwd)
        safe_directory = f"git config --global --add safe.directory {shlex_quote(workspace_container_path)} >/dev/null 2>&1 || true"
        command = f"set -euo pipefail; {safe_directory}; cd {shlex_quote(cwd)} && {stage.command}"
        failure_action = "true" if _is_advisory_stage(stage.name) else f"{cleanup}; exit 0"
        lines.extend(
            [
                f"echo '[rethink] verifier: running {stage.name} timeout={stage.timeout_sec}s'",
                f"echo '__RETHINK_START__ {stage.name}' > /workspace/rethink-logs/{log_name}",
                f"start=$(date +%s)",
                f"timeout {stage.timeout_sec} bash -lc {shlex_quote(command)} >> /workspace/rethink-logs/{log_name} 2>&1",
                "code=$?",
                "end=$(date +%s)",
                f"echo '__RETHINK_END__ {stage.name} exit_code='$code' elapsed_sec='$((end-start)) >> /workspace/rethink-logs/{log_name}",
                f"echo '[rethink] verifier: finished {stage.name} exit_code='$code' elapsed_sec='$((end-start))'s'",
                f"if [ \"$code\" -ne 0 ]; then {failure_action}; fi",
            ]
        )
    lines.append(cleanup)
    lines.append("exit 0")
    return "\n".join(lines)


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _container_cwd(workspace_container_path: str, cwd: str) -> str:
    if cwd in {"", "."}:
        return workspace_container_path
    if cwd.startswith("/"):
        return cwd
    return f"{workspace_container_path.rstrip('/')}/{cwd}"


def _read_sequence_traces(log_paths: list[Path], stages: list[CommandStage], cwd: str, elapsed_total: float) -> list[CommandTrace]:
    traces = []
    for log_path, stage in zip(log_paths, stages):
        text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        recorded_exit_code = _exit_code_from_log(text)
        if recorded_exit_code is None:
            break
        detected_failure = _detect_false_success(stage, text, recorded_exit_code)
        exit_code = detected_failure or recorded_exit_code
        timeout = recorded_exit_code == 124
        elapsed = _elapsed_from_log(text) or elapsed_total
        traces.append(
            build_trace(
                command=stage.command,
                cwd=_container_cwd(cwd, stage.cwd),
                exit_code=None if timeout else exit_code,
                elapsed_sec=elapsed,
                stdout=text,
                stderr=(
                    f"command timed out after {stage.timeout_sec}s"
                    if timeout
                    else "verifier detected failure output despite recorded exit_code=0"
                    if detected_failure
                    else ""
                ),
                timeout=timeout,
                stage=stage.name,
                maturity_target=stage.maturity_target,
            )
        )
        if exit_code != 0 and not _is_advisory_stage(stage.name):
            break
    return traces


def _is_advisory_stage(stage: str | None) -> bool:
    return stage == "strongest_verify"


def _detect_false_success(stage: CommandStage, text: str, exit_code: int) -> int | None:
    if exit_code != 0 or stage.name not in {"minimal_verify", "strongest_verify", "run_probe"}:
        return None
    lowered = text.lower()
    failure_markers = [
        "no module named ",
        "modulenotfounderror",
        "command not found",
        "unrecognized arguments:",
        "error: usage:",
    ]
    return 1 if any(marker in lowered for marker in failure_markers) else None


def _exit_code_from_log(text: str) -> int | None:
    marker = "__RETHINK_END__"
    for line in reversed(text.splitlines()):
        if marker in line and "exit_code=" in line:
            try:
                return int(line.split("exit_code=", 1)[1].split()[0])
            except ValueError:
                return None
    return None


def _elapsed_from_log(text: str) -> float | None:
    marker = "__RETHINK_END__"
    for line in reversed(text.splitlines()):
        if marker in line and "elapsed_sec=" in line:
            try:
                return float(line.split("elapsed_sec=", 1)[1].split()[0])
            except ValueError:
                return None
    return None


def stop_reason_for_trace(trace: CommandTrace) -> StopReason:
    if trace.timeout:
        return StopReason.COMMAND_TIMEOUT
    stderr = trace.stderr_summary.lower()
    if trace.exit_code == 125 and (
        "unable to find image" in stderr
        or "pull access denied" in stderr
        or "cannot connect to the docker daemon" in stderr
    ):
        return StopReason.DOCKER_UNAVAILABLE
    if trace.exit_code != 0:
        return StopReason.VERIFIER_FAILED
    return StopReason.SUCCESS
