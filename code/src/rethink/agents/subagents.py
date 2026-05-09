from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from rethink.discovery import collect_ci_evidence, discover_repo
from rethink.schemas import (
    BootstrapPlan,
    CIEvidenceReport,
    CommandCandidate,
    CommandKind,
    CommandSource,
    DiscoveryReport,
    EvidenceItem,
    Maturity,
    RepairPlan,
    VerifierResult,
)


_PYTHON_SYSTEM_PREREQS = "apt-get update && apt-get install -y python3 python3-venv python3-pip python3-dev build-essential pkg-config"
_JAVA_MAVEN_SYSTEM_PREREQS = "apt-get update && apt-get install -y default-jdk maven"


class DiscoveryAgent:
    name = "DiscoveryAgent"

    def run(self, repo_dir: Path, progress: Callable[[str], None] | None = None) -> DiscoveryReport:
        return discover_repo(repo_dir, progress=progress)


class CIEvidenceAgent:
    name = "CIEvidenceAgent"

    def run(self, repo_dir: Path) -> CIEvidenceReport:
        return collect_ci_evidence(repo_dir)


class CommandPlannerAgent:
    name = "CommandPlannerAgent"

    def run(self, discovery: DiscoveryReport, ci: CIEvidenceReport) -> BootstrapPlan:
        evidence = [*discovery.evidence, *ci.evidence]
        doctor = _doctor_commands(discovery)
        install = _install_commands(discovery)
        minimal = _minimal_verify_command(discovery)
        strongest = _strongest_verify_command(discovery, ci, minimal)
        context = _agent_context(discovery, ci, install, minimal, strongest)
        playbook = "No repair episodes recorded yet.\n"
        return BootstrapPlan(
            repo_name=discovery.repo_name,
            doctor=doctor,
            install=install,
            minimal_verify=minimal,
            strongest_verify=strongest,
            agent_context=context,
            failure_playbook=playbook,
            evidence=evidence,
        )


class RepairAgent:
    name = "RepairAgent"

    def run(self, plan: BootstrapPlan, verifier_result: VerifierResult) -> RepairPlan:
        diagnosis = _diagnose(verifier_result)
        repaired = plan.model_copy(deep=True)
        changed: list[str] = ["failure_playbook.md"]
        commands_changed: list[str] = []

        if "pytest_missing" in diagnosis and not any("pip install pytest" in command.command for command in repaired.install):
            repaired.install.append(
                CommandCandidate(
                    kind=CommandKind.INSTALL,
                    command=".bootstrap/venv/bin/python -m pip install pytest",
                    source=CommandSource.REPAIR,
                    confidence=0.65,
                    timeout_sec=300,
                    maturity_target=Maturity.INSTALLABILITY,
                    reason="Verifier reported missing pytest.",
                )
            )
            commands_changed.append("added .bootstrap/venv/bin/python -m pip install pytest")
            changed.extend(["setup.sh", "commands.yaml", "evidence_map.yaml"])

        repaired.failure_playbook = (repaired.failure_playbook.rstrip() + "\n\n" + _playbook_entry(verifier_result, diagnosis)).strip() + "\n"
        return RepairPlan(
            diagnosis=diagnosis,
            plan=repaired,
            changed_files=sorted(set(changed)),
            commands_added_or_removed=commands_changed,
        )


def _doctor_commands(discovery: DiscoveryReport) -> list[CommandCandidate]:
    commands: list[CommandCandidate] = [
        CommandCandidate(
            kind=CommandKind.DOCTOR,
            command="pwd && ls -la",
            confidence=0.8,
            timeout_sec=30,
            reason="Confirm mounted workspace and visible repository files.",
        )
    ]
    if "Python" in discovery.languages:
        commands.append(_doctor("python --version"))
    if "JavaScript" in discovery.languages:
        commands.append(_doctor("node --version && npm --version"))
    if "Java" in discovery.languages:
        commands.append(_doctor("java -version"))
    return commands


def _doctor(command: str) -> CommandCandidate:
    return CommandCandidate(kind=CommandKind.DOCTOR, command=command, confidence=0.7, timeout_sec=30, reason="Runtime presence check.")


def _install_commands(discovery: DiscoveryReport) -> list[CommandCandidate]:
    files = set(discovery.important_files)
    root_file_names = {Path(item).name for item in files if "/" not in item}
    managers = set(discovery.package_managers)
    commands: list[CommandCandidate] = []

    if "python/pyproject" in managers or "setup.py" in root_file_names:
        commands.append(_install(_PYTHON_SYSTEM_PREREQS, "Install Python runtime, venv support, pip, headers, and common native build tools before creating the bootstrap virtual environment."))
        commands.append(_install("python3 -m venv .bootstrap/venv", "Create isolated Python virtual environment for project dependencies."))
        commands.append(_install(".bootstrap/venv/bin/python -m pip install -U pip wheel", "Upgrade pip and wheel inside the bootstrap virtual environment."))
        commands.append(_install(".bootstrap/venv/bin/python -m pip install -e .", "Install Python project in editable mode inside the bootstrap virtual environment."))
    elif "python/pip" in managers:
        commands.append(_install(_PYTHON_SYSTEM_PREREQS, "Install Python runtime, venv support, pip, headers, and common native build tools before creating the bootstrap virtual environment."))
        commands.append(_install("python3 -m venv .bootstrap/venv", "Create isolated Python virtual environment for project dependencies."))
        commands.append(_install(".bootstrap/venv/bin/python -m pip install -U pip wheel", "Upgrade pip and wheel inside the bootstrap virtual environment."))
        commands.append(_install(".bootstrap/venv/bin/python -m pip install -r requirements.txt", "Install Python requirements inside the bootstrap virtual environment."))

    if "node/pnpm" in managers:
        commands.append(_install("corepack enable && pnpm install --frozen-lockfile", "Install Node dependencies with pnpm lockfile."))
    elif "node/yarn" in managers:
        commands.append(_install("corepack enable && yarn install --frozen-lockfile", "Install Node dependencies with yarn lockfile."))
    elif "node/npm" in managers:
        if "package-lock.json" in root_file_names:
            commands.append(_install("npm ci", "Install Node dependencies from package-lock."))
        else:
            commands.append(_install("npm install", "Install Node dependencies."))

    if "java/maven" in managers:
        commands.append(_install(_JAVA_MAVEN_SYSTEM_PREREQS, "Install Java JDK and Maven before running Maven project commands."))
        commands.append(_install("mvn -q -DskipTests package", "Resolve Maven dependencies and compile package."))
    if "java/gradle" in managers:
        commands.append(_install("apt-get update && apt-get install -y default-jdk", "Install Java JDK before running Gradle project commands."))
        gradle = "./gradlew" if "gradlew" in root_file_names else "gradle"
        commands.append(_install(f"{gradle} assemble", "Resolve Gradle dependencies and assemble project."))
    if "bazel" in managers and not commands:
        commands.append(_install("true", "No lightweight deterministic setup is attempted for Bazel projects; full Bazel builds are too heavyweight for the bootstrap fallback."))
    if "native/autoconf" in managers and not commands:
        commands.append(_install("apt-get update && apt-get install -y build-essential pkg-config", "Install common native build tools for autoconf-style projects without attempting a full build."))
    if "native/cmake" in managers and not commands:
        commands.append(_install("apt-get update && apt-get install -y build-essential cmake pkg-config", "Install common native build tools for CMake projects without attempting a full build."))

    if not commands:
        commands.append(_install("true", "No package manager detected; setup is a no-op."))
    return commands


def _install(command: str, reason: str) -> CommandCandidate:
    return CommandCandidate(
        kind=CommandKind.INSTALL,
        command=command,
        source=CommandSource.HEURISTIC,
        confidence=0.65,
        timeout_sec=600,
        maturity_target=Maturity.INSTALLABILITY,
        reason=reason,
    )


def _minimal_verify_command(discovery: DiscoveryReport) -> CommandCandidate:
    files = set(discovery.important_files)
    package_json = Path(discovery.repo_path) / "package.json"
    if "bazel" in discovery.package_managers:
        return CommandCandidate(
            kind=CommandKind.MINIMAL_VERIFY,
            command="test -f MODULE.bazel -o -f WORKSPACE -o -f WORKSPACE.bazel",
            source=CommandSource.HEURISTIC,
            confidence=0.55,
            timeout_sec=30,
            maturity_target=Maturity.INSTALLABILITY,
            reason="Confirm Bazel workspace metadata is present; full Bazel builds are too heavyweight for minimal bootstrap verification.",
        )
    if "native/autoconf" in discovery.package_managers:
        return CommandCandidate(
            kind=CommandKind.MINIMAL_VERIFY,
            command="test -x configure -o -f configure.ac -o -f Makefile.pre.in",
            source=CommandSource.HEURISTIC,
            confidence=0.55,
            timeout_sec=30,
            maturity_target=Maturity.INSTALLABILITY,
            reason="Confirm autoconf/native build metadata is present; full native builds are too heavyweight for minimal bootstrap verification.",
        )
    if "native/cmake" in discovery.package_managers:
        return CommandCandidate(
            kind=CommandKind.MINIMAL_VERIFY,
            command="test -f CMakeLists.txt",
            source=CommandSource.HEURISTIC,
            confidence=0.55,
            timeout_sec=30,
            maturity_target=Maturity.INSTALLABILITY,
            reason="Confirm CMake project metadata is present; full native builds are too heavyweight for minimal bootstrap verification.",
        )
    if "Python" in discovery.languages:
        tests_dir = Path(discovery.repo_path) / "tests"
        if tests_dir.exists():
            return _verify(".bootstrap/venv/bin/python -m pytest tests", "Run Python test suite directory.", 300)
        return _verify(".bootstrap/venv/bin/python -m compileall .", "Compile Python files as a low-cost smoke test.", 300)
    if "JavaScript" in discovery.languages:
        script = _node_script(package_json, ["test", "build", "lint"])
        return _verify(f"npm run {script}", f"Run npm {script} script.", 600) if script else _verify("npm test", "Run npm test fallback.", 600)
    if "pom.xml" in files:
        return _verify("mvn test", "Run Maven tests.", 900)
    if "build.gradle" in files or "build.gradle.kts" in files:
        gradle = "./gradlew" if "gradlew" in files else "gradle"
        return _verify(f"{gradle} test", "Run Gradle tests.", 900)
    if "Makefile" in files:
        return _verify("make test", "Run Makefile test target by convention.", 600)
    return _verify("true", "No local validation discovered; minimal verify is a no-op.", 30)


def _strongest_verify_command(discovery: DiscoveryReport, ci: CIEvidenceReport, minimal: CommandCandidate) -> CommandCandidate | None:
    for command in ci.local_commands:
        if _looks_like_validation(command, discovery):
            return CommandCandidate(
                kind=CommandKind.STRONGEST_VERIFY,
                command=command,
                source=CommandSource.CI,
                confidence=0.75,
                timeout_sec=1200,
                maturity_target=Maturity.TESTABILITY,
                reason="Validation command extracted from CI workflow.",
            )
    return None


def _verify(command: str, reason: str, timeout: int) -> CommandCandidate:
    return CommandCandidate(
        kind=CommandKind.MINIMAL_VERIFY,
        command=command,
        source=CommandSource.HEURISTIC,
        confidence=0.65,
        timeout_sec=timeout,
        maturity_target=Maturity.TESTABILITY if command != "true" else Maturity.INSTALLABILITY,
        reason=reason,
    )


def _node_script(package_json: Path, preferred: list[str]) -> str | None:
    try:
        package = json.loads(package_json.read_text(encoding="utf-8"))
    except Exception:
        return None
    scripts = package.get("scripts") or {}
    for name in preferred:
        if name in scripts:
            return name
    return None


def _looks_like_validation(command: str, discovery: DiscoveryReport) -> bool:
    lowered = command.lower()
    if "${{" in command or "}}" in command:
        return False
    if any(token in lowered for token in ["curl ", "wget ", "docker ", "gh ", "rename ", "upload-artifact"]):
        return False
    managers = set(discovery.package_managers)
    prefixes_by_manager = {
        "python/pyproject": ("pytest", "python -m pytest", ".bootstrap/venv/bin/python -m pytest", "tox", "nox"),
        "python/pip": ("pytest", "python -m pytest", ".bootstrap/venv/bin/python -m pytest", "tox", "nox"),
        "node/npm": ("npm test", "npm run test", "npm run build", "npm run lint", "npm run typecheck"),
        "node/pnpm": ("pnpm test", "pnpm run test", "pnpm run build", "pnpm run lint", "pnpm run typecheck"),
        "node/yarn": ("yarn test", "yarn run test", "yarn build", "yarn lint"),
        "java/maven": ("mvn test", "mvn -", "./mvnw test", "./mvnw -"),
        "java/gradle": ("gradle test", "./gradlew test", "gradle check", "./gradlew check"),
        "bazel": ("bazel test", "bazel query", "bazelisk test", "bazelisk query"),
    }
    allowed_prefixes: list[str] = []
    for manager in managers:
        allowed_prefixes.extend(prefixes_by_manager.get(manager, ()))
    if not allowed_prefixes and managers:
        return False
    if not allowed_prefixes:
        allowed_prefixes = ["pytest", "npm test", "npm run test", "mvn test", "./mvnw test", "gradle test", "./gradlew test", "make test", "make check", "ctest", "bazel test"]
    normalized = " ".join(command.strip().split()).lower()
    return any(normalized.startswith(prefix) for prefix in allowed_prefixes)


def _agent_context(
    discovery: DiscoveryReport,
    ci: CIEvidenceReport,
    install: list[CommandCandidate],
    minimal: CommandCandidate,
    strongest: CommandCandidate | None,
) -> str:
    install_commands = "\n".join(f"- `{command.command}`" for command in install)
    strongest_line = f"`{strongest.command}`" if strongest else "none"
    return (
        f"# Bootstrap Context\n\n"
        f"Repo: {discovery.repo_name}\n\n"
        f"Detected languages: {', '.join(discovery.languages) or 'unknown'}\n\n"
        f"Package managers: {', '.join(discovery.package_managers) or 'none detected'}\n\n"
        f"Install commands:\n{install_commands}\n\n"
        f"Minimal validation: `{minimal.command}`\n\n"
        f"Strongest local CI-derived validation: {strongest_line}\n\n"
        f"CI workflows inspected: {', '.join(ci.workflows) or 'none'}\n"
    )


def _diagnose(verifier_result: VerifierResult) -> str:
    failed = [trace for trace in verifier_result.traces if trace.exit_code not in (0, None) or trace.timeout]
    if not failed:
        return "unknown_failure"
    text = "\n".join(f"{trace.stdout_summary}\n{trace.stderr_summary}" for trace in failed)
    lowered = text.lower()
    if "no module named pytest" in lowered or "pytest: command not found" in lowered:
        return "pytest_missing"
    if "command not found" in lowered:
        return "missing_command"
    if "permission denied" in lowered:
        return "permission_denied"
    if "timed out" in lowered:
        return "command_timeout"
    return "unknown_failure"


def _playbook_entry(verifier_result: VerifierResult, diagnosis: str) -> str:
    failed = next((trace for trace in verifier_result.traces if trace.exit_code not in (0, None) or trace.timeout), None)
    if not failed:
        return f"## Repair diagnosis: {diagnosis}\nNo failed command was available.\n"
    return (
        f"## Repair diagnosis: {diagnosis}\n\n"
        f"Failed command: `{failed.command}`\n\n"
        f"Exit code: {failed.exit_code}\n\n"
        f"Error summary: {failed.stderr_summary or failed.stdout_summary}\n"
    )
