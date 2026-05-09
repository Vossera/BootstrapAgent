from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Callable

from rethink.schemas import CIEvidenceReport, CommandSource, DiscoveryReport, EvidenceItem


IMPORTANT_NAMES = {
    "README",
    "README.md",
    "README.rst",
    "environment.yml",
    "environment.yaml",
    "environment-dev.yml",
    "environment-dev.yaml",
    "meson.build",
    "meson.options",
    "pixi.toml",
    "pixi.lock",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "setup.py",
    "setup.cfg",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Makefile",
    "Cargo.toml",
    "go.mod",
    "CMakeLists.txt",
    "configure",
    "configure.ac",
    "Makefile.pre.in",
    ".bazelrc",
    ".bazelversion",
    "BUILD",
    "BUILD.bazel",
    "MODULE.bazel",
    "WORKSPACE",
    "WORKSPACE.bazel",
}


IMPORTANT_PATTERNS = {
    "requirements*.txt",
    "requirements*.in",
    "constraints*.txt",
    "environment*.yml",
    "environment*.yaml",
    "tox.ini",
    "noxfile.py",
}


def discover_repo(repo_dir: Path, max_files: int = 80, progress: Callable[[str], None] | None = None) -> DiscoveryReport:
    _emit(progress, "discovery: scanning repository files")
    important_files = _find_important_files(repo_dir, max_files=max_files)
    _emit(progress, f"discovery: found {len(important_files)} important files")
    _emit(progress, "discovery: detecting languages and package managers")
    languages = _detect_languages(repo_dir, important_files)
    package_managers = _detect_package_managers(important_files)
    _emit(progress, "discovery: summarizing project structure")
    structure = _project_structure_evidence(repo_dir)
    _emit(progress, "discovery: collecting file evidence excerpts")
    evidence = [structure, *[_evidence_for_file(repo_dir, rel) for rel in important_files[:40]]]
    _emit(progress, f"discovery: collected {len(evidence)} evidence items")
    return DiscoveryReport(
        repo_name=repo_dir.name,
        repo_path=str(repo_dir),
        languages=languages,
        package_managers=package_managers,
        important_files=important_files,
        evidence=evidence,
        notes=[],
    )


def _emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


def collect_ci_evidence(repo_dir: Path) -> CIEvidenceReport:
    workflow_dir = repo_dir / ".github" / "workflows"
    workflows: list[str] = []
    local_commands: list[str] = []
    non_local_features: list[str] = []
    evidence: list[EvidenceItem] = []

    if workflow_dir.exists():
        for path in sorted(workflow_dir.glob("*")):
            if path.suffix.lower() not in {".yml", ".yaml"}:
                continue
            rel = path.relative_to(repo_dir).as_posix()
            workflows.append(rel)
            text = _read_text(path)
            evidence.append(
                EvidenceItem(
                    source=CommandSource.CI,
                    path=rel,
                    summary="GitHub Actions workflow",
                    excerpt=_excerpt(text),
                )
            )
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("run:") or stripped.startswith("- run:"):
                    command = stripped.split("run:", 1)[1].strip()
                    if command and not command.startswith("|"):
                        local_commands.append(command)
                if any(token in stripped for token in ["secrets.", "services:", "docker:", "aws-", "gcloud", "azure"]):
                    non_local_features.append(f"{rel}: {stripped[:160]}")

    return CIEvidenceReport(
        workflows=workflows,
        local_commands=local_commands[:50],
        non_local_features=non_local_features[:50],
        evidence=evidence,
    )


def _find_important_files(repo_dir: Path, max_files: int) -> list[str]:
    found: list[str] = []
    for path in sorted(repo_dir.rglob("*")):
        if not path.is_file() or _is_ignored(path, repo_dir):
            continue
        rel = path.relative_to(repo_dir).as_posix()
        if _is_important_file(path, rel):
            found.append(rel)
    return sorted(dict.fromkeys(found), key=_important_file_sort_key)[:max_files]


def _is_important_file(path: Path, rel: str) -> bool:
    if path.name in IMPORTANT_NAMES:
        return True
    if any(fnmatch.fnmatch(path.name, pattern) for pattern in IMPORTANT_PATTERNS):
        return True
    return rel.startswith(".github/workflows/") or rel.startswith("docs/")


def _important_file_sort_key(rel: str) -> tuple[int, str]:
    name = Path(rel).name
    if name in {"README.md", "README.rst", "README"} and "/" not in rel:
        return (0, rel)
    root_metadata = {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "meson.build",
        "pixi.toml",
        "environment.yml",
        "environment.yaml",
        ".bazelrc",
        ".bazelversion",
        "BUILD",
        "BUILD.bazel",
        "MODULE.bazel",
        "WORKSPACE",
        "WORKSPACE.bazel",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "package.json",
        "configure",
        "configure.ac",
        "Makefile.pre.in",
        "CMakeLists.txt",
    }
    if name in root_metadata and "/" not in rel:
        return (1, rel)
    if name in root_metadata:
        return (2, rel)
    if fnmatch.fnmatch(name, "requirements*") or fnmatch.fnmatch(name, "constraints*"):
        return (3, rel)
    if rel.startswith(".github/workflows/"):
        return (4, rel)
    if rel.startswith("docs/"):
        return (5, rel)
    if name in {"README.md", "README.rst", "README"}:
        return (6, rel)
    return (7, rel)


def _is_ignored(path: Path, repo_dir: Path) -> bool:
    rel_parts = path.relative_to(repo_dir).parts
    ignored = {".bootstrap", ".git", ".venv", "venv", "node_modules", "dist", "build", "target", "__pycache__"}
    return any(part in ignored for part in rel_parts)


def _detect_languages(repo_dir: Path, important_files: list[str]) -> list[str]:
    languages: list[str] = []
    markers = {
        "Python": ["pyproject.toml", "setup.py", "requirements.txt"],
        "JavaScript": ["package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"],
        "Java": ["pom.xml", "build.gradle", "build.gradle.kts"],
        "Rust": ["Cargo.toml"],
        "Go": ["go.mod"],
        "C/C++": ["CMakeLists.txt", "configure", "configure.ac", "Makefile.pre.in", "BUILD", "BUILD.bazel", "MODULE.bazel", "WORKSPACE", "WORKSPACE.bazel"],
    }
    root_names = {Path(rel).name for rel in important_files if "/" not in rel}
    for language, names in markers.items():
        if any(name in names for name in root_names):
            languages.append(language)

    if not languages:
        for language, names in markers.items():
            if any(Path(rel).name in names for rel in important_files):
                languages.append(language)

    if not languages:
        suffixes = {path.suffix for path in repo_dir.rglob("*") if path.is_file() and not _is_ignored(path, repo_dir)}
        if ".py" in suffixes:
            languages.append("Python")
        if ".js" in suffixes or ".ts" in suffixes:
            languages.append("JavaScript")
        if ".java" in suffixes:
            languages.append("Java")
        if ".rs" in suffixes:
            languages.append("Rust")
        if ".go" in suffixes:
            languages.append("Go")
    return languages


def _detect_package_managers(important_files: list[str]) -> list[str]:
    names = {Path(rel).name for rel in important_files}
    root_names = {Path(rel).name for rel in important_files if "/" not in rel}
    managers: list[str] = []
    if "pyproject.toml" in root_names:
        managers.append("python/pyproject")
    if "requirements.txt" in root_names:
        managers.append("python/pip")
    if "package.json" in root_names:
        if "pnpm-lock.yaml" in root_names:
            managers.append("node/pnpm")
        elif "yarn.lock" in root_names:
            managers.append("node/yarn")
        else:
            managers.append("node/npm")
    if "pom.xml" in root_names:
        managers.append("java/maven")
    if "build.gradle" in root_names or "build.gradle.kts" in root_names:
        managers.append("java/gradle")
    if "Cargo.toml" in root_names:
        managers.append("rust/cargo")
    if "go.mod" in root_names:
        managers.append("go/modules")
    if root_names.intersection({"BUILD", "BUILD.bazel", "MODULE.bazel", "WORKSPACE", "WORKSPACE.bazel", ".bazelrc"}):
        managers.append("bazel")
    if root_names.intersection({"configure", "configure.ac", "Makefile.pre.in"}):
        managers.append("native/autoconf")
    if "CMakeLists.txt" in root_names:
        managers.append("native/cmake")
    if managers:
        return managers
    if "pyproject.toml" in names:
        managers.append("python/pyproject")
    if "requirements.txt" in names:
        managers.append("python/pip")
    if "package.json" in names:
        if "pnpm-lock.yaml" in names:
            managers.append("node/pnpm")
        elif "yarn.lock" in names:
            managers.append("node/yarn")
        else:
            managers.append("node/npm")
    if "pom.xml" in names:
        managers.append("java/maven")
    if "build.gradle" in names or "build.gradle.kts" in names:
        managers.append("java/gradle")
    if "Cargo.toml" in names:
        managers.append("rust/cargo")
    if "go.mod" in names:
        managers.append("go/modules")
    if names.intersection({"BUILD", "BUILD.bazel", "MODULE.bazel", "WORKSPACE", "WORKSPACE.bazel", ".bazelrc"}):
        managers.append("bazel")
    if names.intersection({"configure", "configure.ac", "Makefile.pre.in"}):
        managers.append("native/autoconf")
    if "CMakeLists.txt" in names:
        managers.append("native/cmake")
    return managers


def _evidence_for_file(repo_dir: Path, rel: str) -> EvidenceItem:
    path = repo_dir / rel
    source = _source_for_path(rel)
    summary = _summarize_file(path)
    text = _read_text(path)
    excerpt = _readme_excerpt(text) if source == CommandSource.README else _excerpt(text)
    return EvidenceItem(source=source, path=rel, summary=summary, excerpt=excerpt)


def _source_for_path(rel: str) -> CommandSource:
    name = Path(rel).name
    if name.lower().startswith("readme"):
        return CommandSource.README
    if rel.startswith(".github/workflows/"):
        return CommandSource.CI
    if name in {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "package.json",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "Cargo.toml",
        "go.mod",
        "meson.build",
        "pixi.toml",
        "tox.ini",
        "noxfile.py",
        ".bazelrc",
        ".bazelversion",
        "BUILD",
        "BUILD.bazel",
        "MODULE.bazel",
        "WORKSPACE",
        "WORKSPACE.bazel",
        "configure",
        "configure.ac",
        "Makefile.pre.in",
    }:
        return CommandSource.PACKAGE_METADATA
    if "lock" in name:
        return CommandSource.LOCKFILE
    if name == "Makefile":
        return CommandSource.MAKEFILE
    return CommandSource.DOCS


def _summarize_file(path: Path) -> str:
    if path.name == "pyproject.toml":
        text = _read_text(path)
        backend = _toml_value(text, "build-backend")
        requires = _toml_array_values(text, "requires")
        if backend or requires:
            req_summary = f"; build requirements: {', '.join(requires[:8])}" if requires else ""
            return f"Python project metadata with build backend {backend or 'unknown'}{req_summary}"
    if path.name == "package.json":
        try:
            package = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return "Node package metadata"
        scripts = sorted((package.get("scripts") or {}).keys())
        return f"Node package metadata with scripts: {', '.join(scripts[:12])}" if scripts else "Node package metadata"
    return f"Found {path.name}"


def _project_structure_evidence(repo_dir: Path) -> EvidenceItem:
    top_level = []
    for path in sorted(repo_dir.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
        if _is_ignored(path, repo_dir):
            continue
        suffix = "/" if path.is_dir() else ""
        top_level.append(f"{path.name}{suffix}")
    package_dirs = _python_package_dirs(repo_dir)
    test_dirs = _named_dirs(repo_dir, {"test", "tests"}, limit=12)
    native_files = _native_build_files(repo_dir, limit=20)
    summary_parts = [f"top-level entries: {', '.join(top_level[:40])}"]
    if package_dirs:
        summary_parts.append(f"python package dirs: {', '.join(package_dirs[:20])}")
    if test_dirs:
        summary_parts.append(f"test dirs: {', '.join(test_dirs[:12])}")
    if native_files:
        summary_parts.append(f"native/build files: {', '.join(native_files[:20])}")
    return EvidenceItem(
        source=CommandSource.HEURISTIC,
        path=".",
        summary="Repository structure overview for bootstrap planning",
        excerpt="\n".join(summary_parts),
    )


def _python_package_dirs(repo_dir: Path, limit: int = 20) -> list[str]:
    packages: list[str] = []
    for init_file in sorted(repo_dir.rglob("__init__.py")):
        if _is_ignored(init_file, repo_dir):
            continue
        rel_dir = init_file.parent.relative_to(repo_dir)
        if len(rel_dir.parts) <= 3:
            packages.append(rel_dir.as_posix())
        if len(packages) >= limit:
            break
    return packages


def _named_dirs(repo_dir: Path, names: set[str], limit: int) -> list[str]:
    dirs: list[str] = []
    for path in sorted(repo_dir.rglob("*")):
        if not path.is_dir() or _is_ignored(path, repo_dir):
            continue
        if path.name in names:
            dirs.append(path.relative_to(repo_dir).as_posix())
        if len(dirs) >= limit:
            break
    return dirs


def _native_build_files(repo_dir: Path, limit: int) -> list[str]:
    native_suffixes = {".pyx", ".pxd", ".pxi"}
    names = {"meson.build", "CMakeLists.txt"}
    files: list[str] = []
    for path in sorted(repo_dir.rglob("*")):
        if not path.is_file() or _is_ignored(path, repo_dir):
            continue
        if path.name in names or path.suffix in native_suffixes:
            files.append(path.relative_to(repo_dir).as_posix())
        if len(files) >= limit:
            break
    return files


def _read_text(path: Path, limit: int = 12000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _excerpt(text: str, limit: int = 1200) -> str:
    compact = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    return compact[:limit]


def _readme_excerpt(text: str, limit: int = 1800) -> str:
    targeted = _targeted_lines(
        text,
        keywords={
            "install",
            "installation",
            "build",
            "development",
            "developer",
            "test",
            "testing",
            "contributing",
            "requirements",
            "dependencies",
        },
    )
    return _excerpt(targeted or text, limit=limit)


def _targeted_lines(text: str, keywords: set[str], context: int = 4) -> str:
    lines = text.splitlines()
    selected: set[int] = set()
    for index, line in enumerate(lines):
        lowered = line.lower()
        if any(keyword in lowered for keyword in keywords):
            start = max(0, index - context)
            end = min(len(lines), index + context + 1)
            selected.update(range(start, end))
    return "\n".join(lines[index] for index in sorted(selected))


def _toml_value(text: str, key: str) -> str | None:
    prefix = f"{key} ="
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped.split("=", 1)[1].strip().strip("'\"")
    return None


def _toml_array_values(text: str, key: str) -> list[str]:
    lines = text.splitlines()
    values: list[str] = []
    for index, line in enumerate(lines):
        if not line.strip().startswith(f"{key} = ["):
            continue
        remainder = line.split("[", 1)[1]
        for item_line in [remainder, *lines[index + 1 : index + 20]]:
            if "]" in item_line:
                item_line = item_line.split("]", 1)[0]
                done = True
            else:
                done = False
            for raw in item_line.split(","):
                value = raw.split("#", 1)[0].strip().strip("'\"")
                if value:
                    values.append(value)
            if done:
                return values
    return values
