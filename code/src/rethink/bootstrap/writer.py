from __future__ import annotations

import os
import json
import shutil
from pathlib import Path

from rethink.agents.validators import (
    find_unsafe_reason,
    mutates_bootstrap_contract,
    mutates_install_environment,
    mutates_repo_source,
    requires_external_service,
    uses_system_python_pip_install,
)
from rethink.schemas import BootstrapManifest, BootstrapPlan, CommandCandidate, CommandKind, StopReason
from rethink.serialization import dump_yaml


class BootstrapWriteError(RuntimeError):
    def __init__(self, reason: StopReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def build_manifest(plan: BootstrapPlan) -> BootstrapManifest:
    optional_commands = [command for command in [plan.strongest_verify, plan.run_probe] if command is not None]
    commands = [*plan.doctor, *plan.install, plan.minimal_verify]
    commands.extend(optional_commands)
    safety_warnings = _collect_safety_warnings(plan.doctor, [*plan.install, plan.minimal_verify, *optional_commands])

    files = {
        "doctor.sh": _doctor_script(plan.doctor),
        "setup.sh": _shell_script(plan.install),
        "verify.sh": _verify_script(plan),
        "commands.yaml": dump_yaml(_commands_yaml(plan)),
        "commands.json": json.dumps(_commands_yaml(plan), indent=2, ensure_ascii=False) + "\n",
        "evidence_map.yaml": dump_yaml(_evidence_yaml(plan)),
        "agent_context.md": plan.agent_context or "# Bootstrap Context\n\nNo context generated.\n",
        "failure_playbook.md": plan.failure_playbook or "# Failure Playbook\n\nNo failures recorded.\n",
        "safety_warnings.json": json.dumps(safety_warnings, indent=2, ensure_ascii=False) + "\n",
    }
    return BootstrapManifest(files=files)


def write_bootstrap(repo_dir: Path, manifest: BootstrapManifest) -> Path:
    bootstrap_dir = repo_dir / ".bootstrap"
    if bootstrap_dir.exists():
        shutil.rmtree(bootstrap_dir)
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    for name, content in manifest.files.items():
        path = bootstrap_dir / name
        path.write_text(content, encoding="utf-8")
        if name.endswith(".sh"):
            path.chmod(path.stat().st_mode | 0o755)
    return bootstrap_dir


def _collect_safety_warnings(doctor_commands: list[CommandCandidate], other_commands: list[CommandCandidate]) -> list[dict[str, object]]:
    warnings: list[dict[str, object]] = []
    for candidate in [*doctor_commands, *other_commands]:
        for category, reason in _command_safety_reasons(candidate):
            warnings.append(
                {
                    "category": category,
                    "kind": candidate.kind.value,
                    "cwd": candidate.cwd,
                    "command": candidate.command,
                    "reason": reason,
                }
            )
    for candidate in doctor_commands:
        mutation = mutates_install_environment(candidate.command)
        if mutation:
            warnings.append(
                {
                    "category": "doctor_mutates_install_environment",
                    "kind": candidate.kind.value,
                    "cwd": candidate.cwd,
                    "command": candidate.command,
                    "reason": f"doctor command is expected to be read-only: {mutation}",
                }
            )
    for candidate in other_commands:
        system_pip = uses_system_python_pip_install(candidate.command)
        if system_pip:
            warnings.append(
                {
                    "category": "system_python_pip_install",
                    "kind": candidate.kind.value,
                    "cwd": candidate.cwd,
                    "command": candidate.command,
                    "reason": f"python dependency install does not use .bootstrap/venv: {system_pip}",
                }
            )
    return warnings


def _command_safety_reasons(candidate: CommandCandidate) -> list[tuple[str, str]]:
    checks = [
        ("unsafe_pattern", find_unsafe_reason(candidate.command)),
        ("bootstrap_contract_mutation", mutates_bootstrap_contract(candidate.command)),
        ("repo_source_mutation", mutates_repo_source(candidate.command)),
        ("external_service", requires_external_service(candidate.command)),
    ]
    return [(category, reason) for category, reason in checks if reason]


def _shell_script(commands: list[CommandCandidate]) -> str:
    lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
    if not commands:
        lines.append("true")
    for command in commands:
        lines.append(f"# {command.reason or command.kind.value}")
        lines.append(_with_cwd(command))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _doctor_script(commands: list[CommandCandidate]) -> str:
    lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
    if not commands:
        lines.append("true")
    for command in commands:
        lines.append(f"# {command.reason or command.kind.value}")
        lines.append(_with_cwd(command))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _verify_script(plan: BootstrapPlan) -> str:
    lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
    lines.append(f"# {plan.minimal_verify.reason or plan.minimal_verify.kind.value}")
    lines.append(_with_cwd(plan.minimal_verify))
    lines.append("")
    seen = {plan.minimal_verify.command}
    if plan.strongest_verify and plan.strongest_verify.command not in seen:
        lines.append(f"# Advisory: {plan.strongest_verify.reason or plan.strongest_verify.kind.value}")
        lines.append("set +e")
        lines.append(_with_cwd(plan.strongest_verify))
        lines.append("advisory_code=$?")
        lines.append("set -e")
        lines.append(
            "if [ \"$advisory_code\" -ne 0 ]; then "
            "echo \"[rethink] advisory strongest_verify failed with exit_code=$advisory_code\" >&2; "
            "fi"
        )
        lines.append("")
        seen.add(plan.strongest_verify.command)
    if plan.run_probe and plan.run_probe.command not in seen:
        lines.append(f"# {plan.run_probe.reason or plan.run_probe.kind.value}")
        lines.append(_with_cwd(plan.run_probe))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _with_cwd(command: CommandCandidate) -> str:
    if command.cwd in {"", "."}:
        return command.command
    return f"(cd {os.fsdecode(command.cwd)!r} && {command.command})"


def _commands_yaml(plan: BootstrapPlan) -> dict[str, object]:
    return {
        "repo_name": plan.repo_name,
        "doctor": [command.model_dump(mode="json") for command in plan.doctor],
        "install": [command.model_dump(mode="json") for command in plan.install],
        "minimal_verify": plan.minimal_verify.model_dump(mode="json"),
        "strongest_verify": plan.strongest_verify.model_dump(mode="json") if plan.strongest_verify else None,
        "run_probe": plan.run_probe.model_dump(mode="json") if plan.run_probe else None,
    }


def _evidence_yaml(plan: BootstrapPlan) -> dict[str, object]:
    return {
        "repo_name": plan.repo_name,
        "evidence": [item.model_dump(mode="json") for item in plan.evidence],
    }
