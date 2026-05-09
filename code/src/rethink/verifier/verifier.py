from __future__ import annotations

import json
from pathlib import Path

from rethink.config import RuntimeConfig
from rethink.schemas import CommandCandidate, Maturity, StageResult, StopReason, VerifierResult
from rethink.verifier.docker_runner import CommandStage, DockerRunner, DockerUnavailable, stop_reason_for_trace
from rethink.verifier.policy import maturity_after_script, timeout_for_script


_MATURITY_ORDER = {
    Maturity.NONE: 0,
    Maturity.INSTALLABILITY: 1,
    Maturity.TESTABILITY: 2,
    Maturity.RUNNABILITY: 3,
}


class Verifier:
    def __init__(self, config: RuntimeConfig | None = None) -> None:
        self.config = config or RuntimeConfig()
        self.runner = DockerRunner(self.config)

    def verify(self, repo_dir: Path, log_dir: Path | None = None, round_name: str = "verifier") -> VerifierResult:
        staged = _load_staged_commands(repo_dir, self.config, log_dir, round_name)
        if staged is not None:
            return self._verify_staged(repo_dir, staged)
        return self._verify_legacy(repo_dir, log_dir, round_name)

    def _verify_staged(self, repo_dir: Path, stages: list[CommandStage]) -> VerifierResult:
        try:
            traces = self.runner.run_command_sequence(repo_dir, stages)
        except DockerUnavailable:
            return VerifierResult(status="fail", stop_reason=StopReason.DOCKER_UNAVAILABLE, maturity_reached=Maturity.NONE, traces=[])
        stage_results: list[StageResult] = []
        maturity = Maturity.NONE
        failed_stage: str | None = None
        completed_stages = {trace.stage for trace in traces if trace.stage}
        if traces and traces[0].stage == "verifier_infrastructure":
            trace = traces[0]
            return VerifierResult(
                status="fail",
                stop_reason=stop_reason_for_trace(trace),
                maturity_reached=Maturity.NONE,
                traces=traces,
                stage_results=[
                    StageResult(
                        stage="verifier_infrastructure",
                        status="fail",
                        command=trace.command,
                        exit_code=trace.exit_code,
                        elapsed_sec=trace.elapsed_sec,
                        failure_type=trace.failure_signature.failure_type if trace.failure_signature else None,
                    ),
                    *_append_skipped([], stages, completed_stages),
                ],
                failed_stage="verifier_infrastructure",
                minimal_passed=False,
                strongest_passed=False,
                run_probe_passed=_optional_stage_passed([], stages, "run_probe"),
            )
        for stage, trace in zip(stages, traces):
            if trace.stage == "verifier_infrastructure":
                reason = stop_reason_for_trace(trace)
                stage_results.append(
                    StageResult(
                        stage="verifier_infrastructure",
                        status="fail",
                        command=trace.command,
                        exit_code=trace.exit_code,
                        elapsed_sec=trace.elapsed_sec,
                        failure_type=trace.failure_signature.failure_type if trace.failure_signature else None,
                    )
                )
                return VerifierResult(
                    status="fail",
                    stop_reason=reason,
                    maturity_reached=maturity,
                    traces=traces,
                    stage_results=_append_skipped(stage_results, stages, completed_stages),
                    failed_stage="verifier_infrastructure",
                    minimal_passed=_stage_passed(stage_results, "minimal_verify"),
                    strongest_passed=_stage_passed(stage_results, "strongest_verify"),
                    run_probe_passed=_optional_stage_passed(stage_results, stages, "run_probe"),
                )
            reason = stop_reason_for_trace(trace)
            status = "success" if reason is StopReason.SUCCESS else "fail"
            failure_type = trace.failure_signature.failure_type if trace.failure_signature else None
            stage_results.append(
                StageResult(
                    stage=stage.name,
                    status=status,
                    command=stage.command,
                    exit_code=trace.exit_code,
                    elapsed_sec=trace.elapsed_sec,
                    maturity_target=stage.maturity_target,
                    failure_type=failure_type,
                )
            )
            if reason is not StopReason.SUCCESS:
                if _is_advisory_stage(stage.name):
                    continue
                failed_stage = trace.stage or stage.name
                return VerifierResult(
                    status="fail",
                    stop_reason=reason,
                    maturity_reached=maturity,
                    traces=traces,
                    stage_results=_append_skipped(stage_results, stages, completed_stages),
                    failed_stage=failed_stage,
                    minimal_passed=_stage_passed(stage_results, "minimal_verify"),
                    strongest_passed=_stage_passed(stage_results, "strongest_verify"),
                    run_probe_passed=_optional_stage_passed(stage_results, stages, "run_probe"),
                )
            maturity = _max_maturity(maturity, stage.maturity_target or maturity_after_script(f"{stage.name}.sh"))
        if len(traces) < len(stages):
            missing = stages[len(traces)]
            stage_results = _append_skipped(stage_results, stages, completed_stages)
            return VerifierResult(
                status="fail",
                stop_reason=StopReason.VERIFIER_FAILED,
                maturity_reached=maturity,
                traces=traces,
                stage_results=stage_results,
                failed_stage=missing.name,
                minimal_passed=_stage_passed(stage_results, "minimal_verify"),
                strongest_passed=_stage_passed(stage_results, "strongest_verify"),
                run_probe_passed=_optional_stage_passed(stage_results, stages, "run_probe"),
            )
        return VerifierResult(
            status="success",
            stop_reason=StopReason.SUCCESS,
            maturity_reached=maturity,
            traces=traces,
            stage_results=stage_results,
            failed_stage=None,
            minimal_passed=_stage_passed(stage_results, "minimal_verify"),
            strongest_passed=_stage_passed(stage_results, "strongest_verify"),
            run_probe_passed=_optional_stage_passed(stage_results, stages, "run_probe"),
        )

    def _verify_legacy(self, repo_dir: Path, log_dir: Path | None = None, round_name: str = "verifier") -> VerifierResult:
        script_names = ["setup.sh", "doctor.sh", "verify.sh"]
        scripts = [
            (script, timeout_for_script(script, self.config.budget), _script_log_path(log_dir, round_name, script))
            for script in script_names
        ]
        try:
            traces = self.runner.run_script_sequence(repo_dir, scripts)
        except DockerUnavailable:
            return VerifierResult(status="fail", stop_reason=StopReason.DOCKER_UNAVAILABLE, maturity_reached=Maturity.NONE, traces=[])
        maturity = Maturity.NONE
        for script, trace in zip(script_names, traces):
            reason = stop_reason_for_trace(trace)
            if reason is not StopReason.SUCCESS:
                stage_results = _legacy_stage_results(script_names, traces)
                return VerifierResult(
                    status="fail",
                    stop_reason=reason,
                    maturity_reached=maturity,
                    traces=traces,
                    stage_results=stage_results,
                    failed_stage=trace.stage or script.removesuffix(".sh"),
                    minimal_passed=False,
                    strongest_passed=False,
                )
            if script == "verify.sh" and _verify_script_is_installability_only(repo_dir):
                maturity = Maturity.INSTALLABILITY
            else:
                maturity = maturity_after_script(script)
        if len(traces) < len(script_names):
            return VerifierResult(status="fail", stop_reason=StopReason.VERIFIER_FAILED, maturity_reached=maturity, traces=traces, stage_results=_legacy_stage_results(script_names, traces))
        return VerifierResult(status="success", stop_reason=StopReason.SUCCESS, maturity_reached=maturity, traces=traces, stage_results=_legacy_stage_results(script_names, traces), minimal_passed=True, strongest_passed=True)


def _load_staged_commands(repo_dir: Path, config: RuntimeConfig, log_dir: Path | None, round_name: str) -> list[CommandStage] | None:
    path = repo_dir / ".bootstrap" / "commands.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    stages: list[CommandStage] = [
        CommandStage(
            name="setup",
            command="bash .bootstrap/setup.sh",
            timeout_sec=config.budget.setup_timeout_sec,
            log_path=_stage_log_path(log_dir, round_name, "setup"),
            maturity_target=Maturity.INSTALLABILITY,
        ),
        CommandStage(
            name="doctor",
            command="bash .bootstrap/doctor.sh",
            timeout_sec=config.budget.doctor_timeout_sec,
            log_path=_stage_log_path(log_dir, round_name, "doctor"),
            maturity_target=Maturity.INSTALLABILITY,
        ),
    ]
    minimal = _candidate(data.get("minimal_verify"))
    if minimal is not None:
        stages.append(_stage_from_candidate("minimal_verify", minimal, config.budget.minimal_verify_timeout_sec, _stage_log_path(log_dir, round_name, "minimal_verify")))
    strongest = _candidate(data.get("strongest_verify"))
    if strongest is not None and (minimal is None or strongest.command != minimal.command):
        stages.append(_stage_from_candidate("strongest_verify", strongest, config.budget.strongest_verify_timeout_sec, _stage_log_path(log_dir, round_name, "strongest_verify")))
    run_probe = _candidate(data.get("run_probe"))
    if run_probe is not None:
        stages.append(_stage_from_candidate("run_probe", run_probe, run_probe.timeout_sec or config.budget.minimal_verify_timeout_sec, _stage_log_path(log_dir, round_name, "run_probe")))
    return stages


def _candidate(value: object) -> CommandCandidate | None:
    if not isinstance(value, dict):
        return None
    try:
        return CommandCandidate.model_validate(value)
    except Exception:
        return None


def _stage_from_candidate(name: str, candidate: CommandCandidate, default_timeout: int, log_path: Path | None) -> CommandStage:
    target = candidate.maturity_target
    if name == "strongest_verify" and target == Maturity.NONE:
        target = Maturity.TESTABILITY
    if name == "run_probe" and target == Maturity.NONE:
        target = Maturity.RUNNABILITY
    timeout = default_timeout if name == "strongest_verify" and candidate.timeout_sec == 300 else candidate.timeout_sec
    return CommandStage(
        name=name,
        command=candidate.command,
        timeout_sec=timeout,
        log_path=log_path,
        cwd=candidate.cwd,
        maturity_target=target,
    )


def _stage_log_path(log_dir: Path | None, round_name: str, stage: str) -> Path | None:
    if log_dir is None:
        return None
    return log_dir / f"{round_name}_{stage}.log"


def _max_maturity(left: Maturity, right: Maturity) -> Maturity:
    return right if _MATURITY_ORDER[right] > _MATURITY_ORDER[left] else left


def _stage_passed(results: list[StageResult], stage: str) -> bool:
    return any(result.stage == stage and result.status == "success" for result in results)


def _is_advisory_stage(stage: str | None) -> bool:
    return stage == "strongest_verify"


def _optional_stage_passed(results: list[StageResult], stages: list[CommandStage], stage: str) -> bool | None:
    if not any(item.name == stage for item in stages):
        return None
    return _stage_passed(results, stage)


def _append_skipped(results: list[StageResult], stages: list[CommandStage], completed_stages: set[str | None]) -> list[StageResult]:
    existing = {result.stage for result in results}
    output = list(results)
    for stage in stages:
        if stage.name in existing or stage.name in completed_stages:
            continue
        output.append(StageResult(stage=stage.name, status="skipped", command=stage.command, maturity_target=stage.maturity_target))
    return output


def _legacy_stage_results(script_names: list[str], traces: list[object]) -> list[StageResult]:
    results = []
    for script, trace in zip(script_names, traces):
        exit_code = getattr(trace, "exit_code", None)
        signature = getattr(trace, "failure_signature", None)
        results.append(
            StageResult(
                stage=script.removesuffix(".sh"),
                status="success" if exit_code == 0 else "fail",
                command=getattr(trace, "command", None),
                exit_code=exit_code,
                elapsed_sec=getattr(trace, "elapsed_sec", 0.0),
                failure_type=getattr(signature, "failure_type", None) if signature else None,
            )
        )
    return results


def _script_log_path(log_dir: Path | None, round_name: str, script: str) -> Path | None:
    if log_dir is None:
        return None
    return log_dir / f"{round_name}_{script.removesuffix('.sh')}.log"


def _verify_script_is_installability_only(repo_dir: Path) -> bool:
    try:
        lines = (repo_dir / ".bootstrap" / "verify.sh").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    commands = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("set ") or stripped.startswith("#!"):
            continue
        commands.append(stripped)
    command_tuple = tuple(commands)
    if command_tuple in {
        ("true",),
        ("python3 --version",),
        (".bootstrap/venv/bin/python --version",),
        ("test -f MODULE.bazel -o -f WORKSPACE -o -f WORKSPACE.bazel",),
        ("test -x configure -o -f configure.ac -o -f Makefile.pre.in",),
        ("test -f CMakeLists.txt",),
    }:
        return True
    return len(commands) == 1 and "python" in commands[0] and " -c " in commands[0] and "import sys" in commands[0] and _contains_only_stdlib_imports(commands[0])


def _contains_only_stdlib_imports(command: str) -> bool:
    lowered = _python_inline_code(command).lower()
    stdlib_only = {"sys", "os", "pathlib", "subprocess", "json", "platform", "importlib", "site"}
    imports: list[str] = []
    for part in lowered.replace(";", "\n").splitlines():
        stripped = part.strip().strip("'\"")
        if stripped.startswith("import "):
            imports.extend(item.strip().split()[0].split(".")[0] for item in stripped.removeprefix("import ").split(","))
        if stripped.startswith("from "):
            imports.append(stripped.removeprefix("from ").split()[0].split(".")[0])
    return bool(imports) and all(module in stdlib_only for module in imports)


def _python_inline_code(command: str) -> str:
    if " -c " not in command:
        return command
    code = command.split(" -c ", 1)[1].strip()
    if len(code) >= 2 and code[0] in {"'", '"'}:
        quote = code[0]
        end = code.find(quote, 1)
        if end > 0:
            return code[1:end]
    return code
