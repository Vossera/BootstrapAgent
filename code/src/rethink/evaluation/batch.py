from __future__ import annotations

import csv
from pathlib import Path

from rethink.agents.main import BootstrapOrchestrator
from rethink.config import RuntimeConfig
from rethink.schemas import EvaluationLog


def run_batch(
    csv_path: Path,
    out_root: Path,
    limit: int | None = None,
    verify: bool = True,
    config: RuntimeConfig | None = None,
    allow_fallback: bool = False,
    allow_clone: bool = True,
    repo_root: Path | None = None,
) -> list[EvaluationLog]:
    orchestrator = BootstrapOrchestrator(config=config, allow_fallback=allow_fallback)
    results: list[EvaluationLog] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for index, row in enumerate(reader):
            if limit is not None and index >= limit:
                break
            safe_name = row.get("safe_name") or row.get("name") or f"project_{index}"
            url = row["url"]
            language = row.get("language")
            results.append(
                orchestrator.bootstrap(
                    url,
                    out_root / safe_name,
                    verify=verify,
                    language_hint=language,
                    allow_clone=allow_clone,
                    repo_root=repo_root,
                )
            )
    return results
