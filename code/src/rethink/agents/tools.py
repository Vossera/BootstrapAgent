from __future__ import annotations

from pathlib import Path

from rethink.bootstrap.writer import build_manifest, write_bootstrap
from rethink.schemas import BootstrapManifest, BootstrapPlan


def build_bootstrap_manifest(plan: BootstrapPlan) -> BootstrapManifest:
    return build_manifest(plan)


def write_bootstrap_files(repo_dir: Path, manifest: BootstrapManifest) -> Path:
    return write_bootstrap(repo_dir, manifest)
