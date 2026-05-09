from __future__ import annotations

from pathlib import Path

from rethink.agents.python_policy import PYTHON_BOOTSTRAP_POLICY
from rethink.schemas import BootstrapPlan, DiscoveryReport

MAIN_SYSTEM_PROMPT = """You are the MainBootstrapAgent.

Coordinate specialized subagents to generate a verified .bootstrap package for
an unfamiliar repository. All machine-consumed outputs must be valid JSON or
YAML matching the provided schemas. Do not modify business source code. The
deterministic verifier is the only authority on success or failure.
"""

DISCOVERY_PROMPT = """Collect repository evidence only. Return DiscoveryReport JSON."""

CI_EVIDENCE_PROMPT = """Inspect CI files and return CIEvidenceReport YAML."""

PLANNER_PROMPT = """Generate BootstrapPlan JSON from discovery and CI evidence."""

REPAIR_PROMPT = """Generate RepairPlan JSON from a failed verifier trace."""


BASE_BOOTSTRAP_COMMAND_CONSTRAINTS = """Command constraints:
- Assume the verifier starts from a fresh, minimal Ubuntu container. Do not assume project users already have language runtimes, compilers, package managers, build tools, or test tools installed. If setup/verify needs a tool such as Python, Node, Java, Maven, Gradle, CMake, Ninja, a compiler, pnpm, yarn, Rust, Go, or system headers, install or enable it in `install` commands before first use.
- `doctor` commands run after setup.sh and should normally be read-only health checks of the prepared environment. They may inspect files, print versions, and check PATH. If a repository genuinely requires a late setup/mutation in `doctor`, prefer moving it to `install`; if that is not feasible, keep it explicit in the command/reason and the system will record a safety warning instead of rejecting the plan.
- Put environment setup, package installation, venv creation, dependency installation, and build steps only in `install` commands.
- Every command `reason` must match the actual `command` string. Do not claim a command sets PATH, changes cwd, installs a dependency, uses editable mode, disables build isolation, or runs from outside the repo unless the command actually does that.
- The system writes `.bootstrap/setup.sh`, `.bootstrap/doctor.sh`, `.bootstrap/verify.sh`, and command metadata files from the structured plan. Prefer commands that do not rewrite these generated contract files; if a command does rewrite them, it will be recorded as a safety warning.
- Public downloads with `curl` or `wget` are allowed when they are normal bootstrap inputs, including toolchain installers, release artifacts, source archives, Meson subprojects, or repository dependencies. Prefer pinned versions, checksums, package-manager installs, or extracting into caches such as `/tmp`, `subprojects/packagecache`, or other build caches when practical. Piping downloaded content into a shell is allowed when it is the repository's practical documented setup path, but it will be recorded as a safety warning.
- Repository mutations during setup are allowed when they are part of making the checked-out source buildable, such as initializing submodules, fetching vendored dependencies, populating documented third-party directories, or applying generated files. Prefer native commands such as `git submodule update --init --recursive` over replacing the whole checkout. Mutating `/workspace/repo` will be recorded as a safety warning; do it only when it is necessary for the repository workflow.
- Put validation/import/test commands only in `minimal_verify`, `strongest_verify`, or `run_probe`.
- `minimal_verify` should be the lowest-cost trustworthy project check and is a hard verification gate. `strongest_verify` is advisory: it should be the strongest local CI-derived validation that is reproducible without secrets or external services, but it may fail because upstream tests or heavyweight checks are outside the bootstrap contract. `run_probe` should only be present when evidence identifies a CLI, main entrypoint, dev server, or self-contained runtime workflow.
- Prefer the repository's native development workflow for a fresh source checkout. Do not install a published package with the same name as the repository as a substitute for building, checking, or testing the checked-out source.
- `doctor` commands are health checks after setup. Critical checks such as package import, compiled extension availability, and required build tool presence should fail normally. Only use `|| true` inside a doctor command for explicitly warning-only diagnostics, and say that in the reason.
- If DiscoveryReport contains important files, do not claim the repository is empty or inaccessible. Treat DiscoveryReport as authoritative evidence from the mounted repository.
- Do not use a runtime version check such as `python3 --version`, `node --version`, or `java -version` as `minimal_verify`/`strongest_verify` for a non-empty source repository; that only checks the container, not the project.
- The verifier runs setup.sh, doctor.sh, and verify.sh sequentially in the same Docker container over the mounted repository. Environment changes from setup.sh are available to doctor.sh and verify.sh.
- Prefer `cwd: "."`; do not use host paths such as `runs/...` in commands. The verifier already starts commands in `/workspace/repo`.
- Commands that look risky are not rejected by policy. They are executed and logged in `.bootstrap/safety_warnings.json`; choose the most reproducible command you can, but do not avoid a required bootstrap step merely because it uses curl, wget, submodules, or repository mutation.
"""


PYTHON_PROJECT_PROMPT = f"""Python project profile:
- For Python projects, follow the Python bootstrap policy below. Do not use bare `pip install`, `pip3 install`, or system `python -m pip install` for project dependencies. Use `.bootstrap/venv/bin/python -m pip install ...`, and use `.bootstrap/venv/bin/python` in doctor and verify commands.
- For Python initial plans, do not assume the Docker image already has Python. Put OS runtime/build prerequisites before `python3 -m venv`, usually `apt-get update && apt-get install -y python3 python3-venv python3-pip python3-dev build-essential pkg-config`.
- If a verifier failure says `python3: command not found`, repair by installing Python runtime packages before venv creation; do not switch to bare `python` unless evidence shows that exact executable exists.

{PYTHON_BOOTSTRAP_POLICY}
"""


BAZEL_PROJECT_PROMPT = """Bazel / C/C++ project profile:
- Treat Bazel workspace files such as `MODULE.bazel`, `WORKSPACE`, `.bazelrc`, and `BUILD` as stronger project evidence than nested README files.
- Do not replace a Bazel/C/C++ source repository bootstrap with Python runtime checks. A command such as `python3 --version` does not validate TensorFlow-style repositories.
- Do not `pip install tensorflow`, `pip install torch`, or install a published package with the same name as the source repository unless package metadata explicitly says this repository is a Python package to install that way.
- Full Bazel builds can be too heavyweight for minimal bootstrap. If evidence does not identify a small local target and Bazel is not already available, prefer a bounded metadata/workspace check and set maturity_target to `installability`, not `testability`.
- If using Bazel commands, setup must install or rely on an evidenced Bazel/Bazelisk executable, and verify should be a bounded command such as `bazel query` or a small documented target, not an unbounded full build of `//...`.
"""


NODE_PROJECT_PROMPT = """Node / JavaScript project profile:
- Do not assume Node.js, npm, yarn, pnpm, or corepack already exist. Put the required runtime/package-manager setup in `install` commands before dependency installation or verification.
- Use the package manager implied by lockfiles: `pnpm-lock.yaml` -> pnpm, `yarn.lock` -> yarn, `package-lock.json` -> npm ci, otherwise npm install.
- Prefer the checked-out source workflow: install dependencies in place, then use existing package scripts from `package.json` for verification, in this order when available: test, build, lint, typecheck.
- Do not use Python, Java, or compiler runtime version checks as validation for Node projects.
"""


JAVA_PROJECT_PROMPT = """Java project profile:
- Do not assume the Docker image already has Java, Maven, or Gradle. Put toolchain installation in `install` commands when the verify command needs those tools.
- For Maven projects, prefer the checked-out source workflow with `mvn test` or a bounded documented module test; use Maven setup only when evidence requires a separate dependency/package step.
- For Gradle projects, prefer `./gradlew` when present; otherwise use `gradle`. Prefer `test` for verification and `assemble` only for setup/build evidence.
- Do not use Python or Node runtime version checks as validation for Java projects.
"""


RUST_PROJECT_PROMPT = """Rust project profile:
- Do not assume Rust or Cargo already exist. Put Rust toolchain setup in `install` commands when Cargo commands are needed.
- Use the checked-out source workflow. Prefer `cargo fetch` or `cargo check` for minimal verification when full tests are too expensive, and `cargo test` or a bounded documented test target for strongest local validation.
- Do not `cargo install` a published crate with the repository name as a substitute for validating this source checkout.
- Do not use Python, Node, or Java runtime checks as validation for Rust projects.
"""


GO_PROJECT_PROMPT = """Go project profile:
- Do not assume Go already exists. Put Go toolchain setup in `install` commands when Go commands are needed.
- Use the checked-out source workflow. Prefer `go test ./...` when feasible; if the module is large, use a bounded package or `go test ./... -run` smoke command supported by evidence.
- `go mod download` can be an install/setup command, but runtime/version checks alone are not project validation.
- Do not use Python, Node, or Java runtime checks as validation for Go projects.
"""


NATIVE_PROJECT_PROMPT = """Native C/C++ project profile:
- Do not assume compilers, CMake, Ninja, Meson, pkg-config, autotools, or development headers already exist. Put required build toolchain installation in `install` commands before configure/build steps.
- Prefer repository-native build systems from evidence, such as CMake, Make, Meson, or Bazel.
- Do not use Python runtime checks as validation unless the repository evidence says the project is primarily a Python package.
- If a complete native build is too expensive or missing required system dependencies, choose a bounded metadata/configuration check and mark it as installability.
"""


def bootstrap_command_constraints(discovery: DiscoveryReport | None = None) -> str:
    profiles = _profiles_for_discovery(discovery) if discovery is not None else [
        PYTHON_PROJECT_PROMPT,
        BAZEL_PROJECT_PROMPT,
        NODE_PROJECT_PROMPT,
        JAVA_PROJECT_PROMPT,
        RUST_PROJECT_PROMPT,
        GO_PROJECT_PROMPT,
        NATIVE_PROJECT_PROMPT,
    ]
    return "\n\n".join([BASE_BOOTSTRAP_COMMAND_CONSTRAINTS, *profiles]).strip() + "\n"


def repair_command_constraints(plan: BootstrapPlan) -> str:
    profiles = _profiles_for_plan(plan)
    return "\n\n".join([BASE_BOOTSTRAP_COMMAND_CONSTRAINTS, *profiles]).strip() + "\n"


def _profiles_for_discovery(discovery: DiscoveryReport) -> list[str]:
    profiles: list[str] = []
    managers = set(discovery.package_managers)
    languages = set(discovery.languages)
    root_names = _root_path_names(item for item in discovery.important_files)
    if "bazel" in managers:
        profiles.append(BAZEL_PROJECT_PROMPT)
    if _is_python_project(languages, managers, root_names):
        profiles.append(PYTHON_PROJECT_PROMPT)
    if "JavaScript" in languages or any(manager.startswith("node/") for manager in managers):
        profiles.append(NODE_PROJECT_PROMPT)
    if "Java" in languages or any(manager.startswith("java/") for manager in managers):
        profiles.append(JAVA_PROJECT_PROMPT)
    if "Rust" in languages or "Cargo.toml" in root_names:
        profiles.append(RUST_PROJECT_PROMPT)
    if "Go" in languages or "go.mod" in root_names:
        profiles.append(GO_PROJECT_PROMPT)
    if "C/C++" in languages and "bazel" not in managers:
        profiles.append(NATIVE_PROJECT_PROMPT)
    return profiles


def _profiles_for_plan(plan: BootstrapPlan) -> list[str]:
    command_text = "\n".join(
        [
            *(command.command for command in plan.install),
            plan.minimal_verify.command,
            plan.strongest_verify.command if plan.strongest_verify else "",
            plan.run_probe.command if plan.run_probe else "",
        ]
    ).lower()
    evidence_paths = [item.path for item in plan.evidence]
    paths = {Path(item).name.lower() for item in evidence_paths}
    root_names = _root_path_names(evidence_paths)
    profiles: list[str] = []
    if paths.intersection({"module.bazel", "workspace", "workspace.bazel", "build", "build.bazel", ".bazelrc"}) or "bazel " in command_text:
        profiles.append(BAZEL_PROJECT_PROMPT)
    if _is_python_project(set(), set(), root_names) or _uses_python_project_commands(command_text):
        profiles.append(PYTHON_PROJECT_PROMPT)
    if root_names.intersection({"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"}) or any(token in command_text for token in ["npm ", "pnpm ", "yarn "]):
        profiles.append(NODE_PROJECT_PROMPT)
    if root_names.intersection({"pom.xml", "build.gradle", "build.gradle.kts", "gradlew"}) or any(token in command_text for token in ["mvn ", "gradle ", "./gradlew"]):
        profiles.append(JAVA_PROJECT_PROMPT)
    if root_names.intersection({"cargo.toml", "cargo.lock"}) or "cargo " in command_text:
        profiles.append(RUST_PROJECT_PROMPT)
    if root_names.intersection({"go.mod", "go.sum"}) or any(token in command_text for token in ["go test", "go mod", "go build"]):
        profiles.append(GO_PROJECT_PROMPT)
    if paths.intersection({"cmakelists.txt", "makefile"}) or any(token in command_text for token in ["cmake", "make "]):
        profiles.append(NATIVE_PROJECT_PROMPT)
    return profiles


def _root_path_names(paths: object) -> set[str]:
    names: set[str] = set()
    for item in paths:
        path = Path(str(item))
        if len(path.parts) == 1:
            names.add(path.name)
    return names


def _is_python_project(languages: set[str], managers: set[str], root_names: set[str]) -> bool:
    python_root_files = {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "requirements-dev.txt",
        "tox.ini",
        "pytest.ini",
    }
    if root_names.intersection(python_root_files):
        return True
    if any(manager.startswith("python/") for manager in managers):
        return True
    return "Python" in languages and bool(root_names.intersection(python_root_files))


def _uses_python_project_commands(command_text: str) -> bool:
    return any(
        token in command_text
        for token in [
            "pip install -e .",
            "pip install .",
            "python -m pip install -e .",
            "python -m pip install .",
            "python3 -m pip install -e .",
            "python3 -m pip install .",
            "pytest",
            "tox",
        ]
    )
