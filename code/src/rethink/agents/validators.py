from __future__ import annotations

import re


UNSAFE_PATTERNS = [
    re.compile(r"\brm\s+-rf\s+/(?:\s|$)"),
    re.compile(r"\bmkfs(?:\.\w+)?\b"),
    re.compile(r"\bdd\s+if="),
    re.compile(r"\bdocker\s+run\b.*\s--privileged\b"),
    re.compile(r"\bchmod\s+-R\s+777\s+/(?:\s|$)"),
    re.compile(r"\bmount\b.*\s/(?:\s|$)"),
    re.compile(r"\b(curl|wget)\b.*\|\s*(sudo\s+)?(sh|bash)\b"),
]


EXTERNAL_SERVICE_PATTERNS = [
    re.compile(r"\baws\b|\bgcloud\b|\baz\b"),
    re.compile(r"\bterraform\b"),
    re.compile(r"\bkubectl\b"),
    re.compile(r"\bheroku\b"),
]


INSTALL_MUTATION_PATTERNS = [
    re.compile(r"\b(?:apt-get|apt|dnf|yum|apk|brew)\s+.*\binstall\b"),
    re.compile(r"\b(?:pip|pip3)\s+install\b"),
    re.compile(r"\bpython(?:3)?\s+-m\s+pip\s+install\b"),
    re.compile(r"\b(?:npm|pnpm|yarn)\s+(?:install|ci|add)\b"),
    re.compile(r"\b(?:bundle|gem)\s+install\b"),
    re.compile(r"\b(?:cargo|go)\s+install\b"),
    re.compile(r"\b(?:conda|mamba|micromamba)\s+(?:install|create)\b"),
]


BOOTSTRAP_CONTRACT_MUTATION_PATTERNS = [
    re.compile(r"(?:^|[;&|]\s*)(?:cat|printf|echo)\b[^;&|]*(?:>|>>)\s*['\"]?\.bootstrap/(?:setup|doctor|verify)\.sh['\"]?"),
    re.compile(r"(?:^|[;&|]\s*)tee(?:\s+-a)?\s+['\"]?\.bootstrap/(?:setup|doctor|verify)\.sh['\"]?"),
    re.compile(r"(?:^|[;&|]\s*)chmod\b[^;&|]*\.bootstrap/(?:setup|doctor|verify)\.sh\b"),
    re.compile(r"(?:open|write_text|write_bytes)\s*\([^)]*\.bootstrap/(?:setup|doctor|verify)\.sh", re.DOTALL),
    re.compile(r"\.bootstrap/(?:setup|doctor|verify)\.sh['\"]?\s*\)\s*\.\s*(?:write_text|write_bytes|open)\s*\(", re.DOTALL),
    re.compile(r"\.bootstrap/(?:setup|doctor|verify)\.sh[^;&|]*(?:write|writelines|truncate)\s*\(", re.DOTALL),
]


REPO_MUTATION_PATTERNS = [
    re.compile(r"\bcp\s+(?:-[\w-]+\s+)*[^;&|]*\s/workspace/repo(?:/|\b)"),
    re.compile(r"\bmv\s+[^;&|]*\s/workspace/repo(?:/|\b)"),
    re.compile(r"\btar\s+[^;&|]*(?:-C\s+/workspace/repo|/workspace/repo)"),
    re.compile(r"\brsync\b[^;&|]*\s/workspace/repo(?:/|\b)"),
    re.compile(r"\bgit\s+clone\b[^;&|]*\s/workspace/repo(?:/|\b)"),
    re.compile(r"(?:>|>>)\s*/workspace/repo/"),
]


REMOTE_SOURCE_ARCHIVE_PATTERNS = [
    re.compile(r"\b(?:curl|wget)\b[^;&|]*(?:github\.com|archive\.apache\.org|codeload\.github\.com|.*\.(?:tar\.gz|tgz|zip))"),
]


def find_unsafe_reason(command: str) -> str | None:
    for pattern in UNSAFE_PATTERNS:
        if pattern.search(command):
            return f"matched unsafe pattern: {pattern.pattern}"
    return None


def requires_external_service(command: str) -> str | None:
    for pattern in EXTERNAL_SERVICE_PATTERNS:
        if pattern.search(command):
            return f"matched external service pattern: {pattern.pattern}"
    return None


def mutates_install_environment(command: str) -> str | None:
    for pattern in INSTALL_MUTATION_PATTERNS:
        if pattern.search(command):
            return f"matched install mutation pattern: {pattern.pattern}"
    return None


def uses_system_python_pip_install(command: str) -> str | None:
    if ".bootstrap/venv/" in command or "source .bootstrap/venv/bin/activate" in command or ". .bootstrap/venv/bin/activate" in command:
        return None
    patterns = [
        re.compile(r"(?<![\w./-])(?:pip|pip3)\s+install\b"),
        re.compile(r"(?<![\w./-])python(?:3)?\s+-m\s+pip\s+install\b"),
    ]
    for pattern in patterns:
        if pattern.search(command):
            return f"matched system pip install pattern: {pattern.pattern}"
    return None


def mutates_bootstrap_contract(command: str) -> str | None:
    for pattern in BOOTSTRAP_CONTRACT_MUTATION_PATTERNS:
        if pattern.search(command):
            return f"matched bootstrap contract mutation pattern: {pattern.pattern}"
    return None


def mutates_repo_source(command: str) -> str | None:
    for pattern in REPO_MUTATION_PATTERNS:
        if pattern.search(command):
            return f"matched repo source mutation pattern: {pattern.pattern}"
    return None


def downloads_source_archive(command: str) -> str | None:
    for pattern in REMOTE_SOURCE_ARCHIVE_PATTERNS:
        if pattern.search(command):
            return f"matched remote source archive pattern: {pattern.pattern}"
    return None
