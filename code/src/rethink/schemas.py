from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class CommandKind(str, Enum):
    DOCTOR = "doctor"
    INSTALL = "install"
    MINIMAL_VERIFY = "minimal_verify"
    STRONGEST_VERIFY = "strongest_verify"
    RUN_PROBE = "run_probe"


class CommandSource(str, Enum):
    README = "readme"
    CI = "ci"
    PACKAGE_METADATA = "package_metadata"
    LOCKFILE = "lockfile"
    MAKEFILE = "makefile"
    DOCS = "docs"
    REPAIR = "repair"
    HEURISTIC = "heuristic"


class Maturity(str, Enum):
    NONE = "none"
    INSTALLABILITY = "installability"
    TESTABILITY = "testability"
    RUNNABILITY = "runnability"


class StopReason(str, Enum):
    SUCCESS = "success"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    MAX_REPAIR_LOOPS_REACHED = "max_repair_loops_reached"
    MAX_TOTAL_WALL_TIME_REACHED = "max_total_wall_time_reached"
    MAX_SHELL_COMMANDS_REACHED = "max_shell_commands_reached"
    COMMAND_TIMEOUT = "command_timeout"
    REPEATED_SAME_FAILURE = "repeated_same_failure"
    STRONGEST_TEST_REPAIR_LIMIT_REACHED = "strongest_test_repair_limit_reached"
    CLEAN_REPLAY_REPAIR_LIMIT_REACHED = "clean_replay_repair_limit_reached"
    UNSAFE_COMMAND_DETECTED = "unsafe_command_detected"
    EXTERNAL_SERVICE_REQUIRED = "external_service_required"
    DOCKER_UNAVAILABLE = "docker_unavailable"
    VERIFIER_FAILED = "verifier_failed"
    LLM_UNAVAILABLE = "llm_unavailable"


class RepoInput(BaseModel):
    source: str
    name: str
    is_url: bool = False
    language_hint: str | None = None


class EvidenceItem(BaseModel):
    source: CommandSource | str
    path: str
    summary: str
    excerpt: str | None = None
    executed: bool = False


class DiscoveryReport(BaseModel):
    repo_name: str
    repo_path: str
    languages: list[str] = Field(default_factory=list)
    package_managers: list[str] = Field(default_factory=list)
    important_files: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CIEvidenceReport(BaseModel):
    workflows: list[str] = Field(default_factory=list)
    local_commands: list[str] = Field(default_factory=list)
    non_local_features: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)


class CommandCandidate(BaseModel):
    kind: CommandKind
    cwd: str = "."
    command: str
    source: CommandSource | str = CommandSource.HEURISTIC
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    timeout_sec: int = Field(default=300, ge=1)
    maturity_target: Maturity = Maturity.NONE
    reason: str = ""

    @field_validator("command")
    @classmethod
    def command_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("command cannot be empty")
        return value


class BootstrapPlan(BaseModel):
    repo_name: str
    doctor: list[CommandCandidate] = Field(default_factory=list)
    install: list[CommandCandidate] = Field(default_factory=list)
    minimal_verify: CommandCandidate
    strongest_verify: CommandCandidate | None = None
    run_probe: CommandCandidate | None = None
    agent_context: str = ""
    failure_playbook: str = ""
    evidence: list[EvidenceItem] = Field(default_factory=list)


class BootstrapManifest(BaseModel):
    files: dict[str, str]

    @field_validator("files")
    @classmethod
    def only_bootstrap_files(cls, value: dict[str, str]) -> dict[str, str]:
        allowed = {
            "setup.sh",
            "verify.sh",
            "doctor.sh",
            "commands.yaml",
            "commands.json",
            "evidence_map.yaml",
            "agent_context.md",
            "failure_playbook.md",
            "safety_warnings.json",
        }
        extra = set(value) - allowed
        if extra:
            raise ValueError(f"unexpected bootstrap files: {sorted(extra)}")
        return value


class FailureSignature(BaseModel):
    command: str
    cwd: str
    exit_code: int | None = None
    normalized_error_snippet: str = ""
    failure_type: str = "unknown"


class CommandTrace(BaseModel):
    command: str
    cwd: str
    exit_code: int | None
    elapsed_sec: float
    stage: str | None = None
    maturity_target: Maturity | None = None
    stdout_summary: str = ""
    stderr_summary: str = ""
    timeout: bool = False
    failure_signature: FailureSignature | None = None


class StageResult(BaseModel):
    stage: str
    status: Literal["success", "fail", "skipped"]
    command: str | None = None
    exit_code: int | None = None
    elapsed_sec: float = 0.0
    maturity_target: Maturity | None = None
    failure_type: str | None = None


class VerifierResult(BaseModel):
    status: Literal["success", "fail"]
    stop_reason: StopReason
    maturity_reached: Maturity = Maturity.NONE
    traces: list[CommandTrace] = Field(default_factory=list)
    stage_results: list[StageResult] = Field(default_factory=list)
    failed_stage: str | None = None
    minimal_passed: bool = False
    strongest_passed: bool = False
    run_probe_passed: bool | None = None


class RepairPlan(BaseModel):
    diagnosis: str
    plan: BootstrapPlan
    changed_files: list[str] = Field(default_factory=list)
    commands_added_or_removed: list[str] = Field(default_factory=list)


class EvaluationLog(BaseModel):
    status: Literal["success", "fail"]
    bootstrap_path: str
    warm_status: Literal["success", "fail"] | None = None
    warm_stop_reason: StopReason | None = None
    clean_replay_status: Literal["success", "fail"] | None = None
    clean_replay_stop_reason: StopReason | None = None
    clean_replay_repair_count: int = 0
    minimal_command: str | None = None
    strongest_local_ci_command: str | None = None
    run_probe_command: str | None = None
    maturity_reached: Maturity = Maturity.NONE
    failed_stage: str | None = None
    minimal_passed: bool = False
    strongest_passed: bool = False
    run_probe_passed: bool | None = None
    stage_results: list[StageResult] = Field(default_factory=list)
    token_cost: float | None = None
    wall_clock_time_sec: float = 0.0
    command_count: int = 0
    retry_count: int = 0
    stop_reason: StopReason
    trace_files: list[str] = Field(default_factory=list)
    agent_output_files: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def model_to_json(model: BaseModel) -> str:
    return model.model_dump_json(indent=2)


def path_to_str(path: Path) -> str:
    return str(path)
