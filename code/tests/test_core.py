from __future__ import annotations

import tempfile
import unittest
import importlib.util
import json
import argparse
import sys
import time
from pathlib import Path
from unittest.mock import patch

from rethink.agents.llm import DeepAgentsStructuredClient, RepairPlanDelta, _BOOTSTRAP_COMMAND_CONSTRAINTS, _compact_bootstrap_plan, _compact_verifier_result
from rethink.agents.main import BootstrapOrchestrator, PlanSanityError, _validate_plan_against_discovery, _validate_strongest_not_downgraded
from rethink.agents.prompts import bootstrap_command_constraints, repair_command_constraints
from rethink.agents.subagents import CommandPlannerAgent
from rethink.agents.validators import downloads_source_archive, find_unsafe_reason, mutates_bootstrap_contract, mutates_repo_source
from rethink.bootstrap.writer import build_manifest, write_bootstrap
from rethink.config import RunPaths, RuntimeConfig
from rethink.discovery import collect_ci_evidence, discover_repo
from rethink.repo import find_cached_repo, parse_repo_input, prepare_workspace
from rethink.schemas import CommandCandidate, CommandKind, CommandTrace, Maturity, BootstrapPlan, RepairPlan, StageResult, StopReason, VerifierResult
from rethink.verifier.docker_runner import CommandStage, DockerRunner, WarmDockerRunner, _read_sequence_traces, _sequence_script
from rethink.verifier.verifier import Verifier

_RUN_CSV_SLICE_SPEC = importlib.util.spec_from_file_location("run_csv_slice", Path(__file__).resolve().parents[1] / "scripts" / "run_csv_slice.py")
assert _RUN_CSV_SLICE_SPEC is not None and _RUN_CSV_SLICE_SPEC.loader is not None
_RUN_CSV_SLICE = importlib.util.module_from_spec(_RUN_CSV_SLICE_SPEC)
sys.modules["run_csv_slice"] = _RUN_CSV_SLICE
_RUN_CSV_SLICE_SPEC.loader.exec_module(_RUN_CSV_SLICE)

_CLONE_BENCHMARK_SPEC = importlib.util.spec_from_file_location("clone_benchmark_repos", Path(__file__).resolve().parents[1] / "scripts" / "clone_benchmark_repos.py")
assert _CLONE_BENCHMARK_SPEC is not None and _CLONE_BENCHMARK_SPEC.loader is not None
_CLONE_BENCHMARK = importlib.util.module_from_spec(_CLONE_BENCHMARK_SPEC)
sys.modules["clone_benchmark_repos"] = _CLONE_BENCHMARK
_CLONE_BENCHMARK_SPEC.loader.exec_module(_CLONE_BENCHMARK)


class CoreTests(unittest.TestCase):
    def test_benchmark_scripts_default_to_data3_cache(self) -> None:
        expected = Path("/artifact/local_data/benchmark")

        self.assertEqual(_RUN_CSV_SLICE.DEFAULT_BENCHMARK_ROOT, expected)
        self.assertEqual(_CLONE_BENCHMARK.DEFAULT_BENCHMARK_ROOT, expected)

    def test_unsafe_command_detector(self) -> None:
        self.assertIsNotNone(find_unsafe_reason("rm -rf /"))
        self.assertIsNone(find_unsafe_reason("rm -rf .bootstrap"))

    def test_bootstrap_contract_mutation_detector(self) -> None:
        self.assertIsNotNone(mutates_bootstrap_contract("cat > .bootstrap/setup.sh <<'EOF'\ntrue\nEOF"))
        self.assertIsNotNone(mutates_bootstrap_contract("tee .bootstrap/doctor.sh <<'EOF'\ntrue\nEOF"))
        self.assertIsNotNone(mutates_bootstrap_contract("chmod +x .bootstrap/verify.sh"))
        self.assertIsNotNone(mutates_bootstrap_contract("python3 -c \"from pathlib import Path; Path('.bootstrap/setup.sh').write_text('true')\""))
        self.assertIsNone(mutates_bootstrap_contract("bash .bootstrap/setup.sh"))

    def test_repo_source_mutation_detector(self) -> None:
        self.assertIsNotNone(mutates_repo_source("cp -r /tmp/src/* /workspace/repo/"))
        self.assertIsNotNone(mutates_repo_source("tar xzf src.tgz -C /workspace/repo"))
        self.assertIsNone(mutates_repo_source("tar xzf src.tgz -C /tmp"))

    def test_source_archive_download_detector(self) -> None:
        self.assertIsNotNone(downloads_source_archive("wget https://archive.apache.org/dist/commons/foo.tar.gz -O /tmp/foo.tgz"))
        self.assertIsNotNone(downloads_source_archive("curl -L https://github.com/org/repo/archive/refs/heads/main.zip -o /tmp/src.zip"))
        self.assertIsNone(downloads_source_archive("apt-get update && apt-get install -y default-jdk maven"))

    def test_discovery_detects_python_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
            (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
            report = discover_repo(repo)
            self.assertIn("Python", report.languages)
            self.assertIn("python/pyproject", report.package_managers)

    def test_discovery_detects_bazel_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "MODULE.bazel").write_text("module(name = 'fixture')\n", encoding="utf-8")
            (repo / "BUILD").write_text("cc_library(name = 'fixture')\n", encoding="utf-8")

            report = discover_repo(repo)

            self.assertIn("C/C++", report.languages)
            self.assertIn("bazel", report.package_managers)
            self.assertIn("MODULE.bazel", report.important_files)

    def test_discovery_detects_rust_and_go_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "Cargo.toml").write_text("[package]\nname='fixture'\nversion='0.1.0'\n", encoding="utf-8")
            (repo / "src").mkdir()
            (repo / "src" / "lib.rs").write_text("pub fn f() {}\n", encoding="utf-8")

            report = discover_repo(repo)

            self.assertIn("Rust", report.languages)
            self.assertIn("rust/cargo", report.package_managers)

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "go.mod").write_text("module example.com/fixture\n", encoding="utf-8")
            (repo / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")

            report = discover_repo(repo)

            self.assertIn("Go", report.languages)
            self.assertIn("go/modules", report.package_managers)

    def test_discovery_prioritizes_root_build_files_over_nested_python_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
            (repo / "pom.xml").write_text("<project />\n", encoding="utf-8")
            nested = repo / "module-python"
            nested.mkdir()
            (nested / "pyproject.toml").write_text("[project]\nname='nested'\n", encoding="utf-8")
            for index in range(100):
                doc = repo / f"docs-{index}" / "README.md"
                doc.parent.mkdir()
                doc.write_text("# docs\n", encoding="utf-8")

            report = discover_repo(repo)

            self.assertEqual(report.important_files[:2], ["README.md", "pom.xml"])
            self.assertIn("Java", report.languages)
            self.assertIn("java/maven", report.package_managers)

    def test_discovery_detects_autoconf_native_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "README.rst").write_text("Native project\n", encoding="utf-8")
            (repo / "configure").write_text("#!/bin/sh\n", encoding="utf-8")
            (repo / "Makefile.pre.in").write_text("all:\n", encoding="utf-8")
            (repo / "submodule").mkdir()
            (repo / "submodule" / "pyproject.toml").write_text("[project]\nname='nested'\n", encoding="utf-8")

            report = discover_repo(repo)

            self.assertIn("C/C++", report.languages)
            self.assertIn("native/autoconf", report.package_managers)
            self.assertNotIn("python/pyproject", report.package_managers)
            self.assertLess(report.important_files.index("configure"), report.important_files.index("submodule/pyproject.toml"))

    def test_fallback_planner_uses_native_metadata_verify_for_autoconf_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "README.rst").write_text("Native project\n", encoding="utf-8")
            (repo / "configure").write_text("#!/bin/sh\n", encoding="utf-8")
            (repo / "Makefile.pre.in").write_text("all:\n", encoding="utf-8")
            discovery = discover_repo(repo)

            plan = CommandPlannerAgent().run(discovery, collect_ci_evidence(repo))

            self.assertIn("build-essential", " && ".join(command.command for command in plan.install))
            self.assertEqual(plan.minimal_verify.command, "test -x configure -o -f configure.ac -o -f Makefile.pre.in")
            self.assertEqual(plan.minimal_verify.maturity_target, Maturity.INSTALLABILITY)
            self.assertIsNone(plan.strongest_verify)

    def test_fallback_planner_ignores_nested_python_setup_for_native_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "README.rst").write_text("Native project\n", encoding="utf-8")
            (repo / "configure").write_text("#!/bin/sh\n", encoding="utf-8")
            (repo / "Makefile.pre.in").write_text("all:\n", encoding="utf-8")
            nested = repo / "Lib" / "test"
            nested.mkdir(parents=True)
            (nested / "setup.py").write_text("from setuptools import setup\n", encoding="utf-8")
            discovery = discover_repo(repo)

            plan = CommandPlannerAgent().run(discovery, collect_ci_evidence(repo))

            commands = [command.command for command in plan.install]
            self.assertFalse(any("pip install ." in command for command in commands))
            self.assertFalse(any("python3 -m venv" in command for command in commands))

    def test_fallback_planner_filters_github_expression_ci_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pom.xml").write_text("<project />\n", encoding="utf-8")
            workflow = repo / ".github" / "workflows"
            workflow.mkdir(parents=True)
            (workflow / "ci.yml").write_text(
                "jobs:\n"
                "  test:\n"
                "    steps:\n"
                "      - run: find ${{ steps.test-run.outputs.debug-files-output-dir }} -type f -exec rename 's/x/y/' {} \\;\n",
                encoding="utf-8",
            )
            discovery = discover_repo(repo)

            plan = CommandPlannerAgent().run(discovery, collect_ci_evidence(repo))

            self.assertIsNone(plan.strongest_verify)

    def test_discovery_includes_project_structure_and_targeted_readme(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            package = repo / "fixture" / "_libs"
            tests = repo / "tests"
            package.mkdir(parents=True)
            tests.mkdir()
            (repo / "fixture" / "__init__.py").write_text("", encoding="utf-8")
            (package / "algos.pyx").write_text("# cython\n", encoding="utf-8")
            (tests / "test_basic.py").write_text("def test_basic(): pass\n", encoding="utf-8")
            (repo / "README.md").write_text(
                "# Fixture\n\n"
                "Intro that should not dominate.\n\n"
                "## Development install\n\n"
                "Run python -m pip install -e . and python -m pytest.\n",
                encoding="utf-8",
            )
            (repo / "pyproject.toml").write_text(
                "[build-system]\n"
                "requires = [\n"
                "  'meson-python',\n"
                "  'Cython',\n"
                "]\n"
                "build-backend = 'mesonpy'\n",
                encoding="utf-8",
            )
            (repo / "meson.build").write_text("project('fixture')\n", encoding="utf-8")

            report = discover_repo(repo)

            structure = next(item for item in report.evidence if item.path == ".")
            self.assertIn("fixture/", structure.excerpt or "")
            self.assertIn("python package dirs: fixture", structure.excerpt or "")
            self.assertIn("native/build files:", structure.excerpt or "")
            self.assertLess(report.important_files.index("README.md"), report.important_files.index("meson.build"))
            self.assertIn("meson.build", report.important_files)
            readme = next(item for item in report.evidence if item.path == "README.md")
            self.assertIn("Development install", readme.excerpt or "")
            pyproject = next(item for item in report.evidence if item.path == "pyproject.toml")
            self.assertIn("build backend mesonpy", pyproject.summary)
            self.assertIn("meson-python", pyproject.summary)

    def test_discovery_reports_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
            events: list[str] = []

            discover_repo(repo, progress=events.append)

            self.assertIn("discovery: scanning repository files", events)
            self.assertTrue(any(event.startswith("discovery: found ") for event in events))
            self.assertIn("discovery: collecting file evidence excerpts", events)
            self.assertTrue(events[-1].startswith("discovery: collected "))

    def test_ci_evidence_extracts_run_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            workflow = repo / ".github" / "workflows"
            workflow.mkdir(parents=True)
            (workflow / "ci.yml").write_text("jobs:\n  test:\n    steps:\n      - run: python -m pytest\n", encoding="utf-8")
            report = collect_ci_evidence(repo)
            self.assertEqual(report.local_commands, ["python -m pytest"])

    def test_bootstrap_writer_outputs_contract_files(self) -> None:
        plan = BootstrapPlan(
            repo_name="fixture",
            install=[CommandCandidate(kind=CommandKind.INSTALL, command="true", maturity_target=Maturity.INSTALLABILITY)],
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="true", maturity_target=Maturity.INSTALLABILITY),
        )
        manifest = build_manifest(plan)
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap = write_bootstrap(Path(tmp), manifest)
            names = {path.name for path in bootstrap.iterdir()}
            self.assertEqual(
                names,
                {
                    "setup.sh",
                    "verify.sh",
                    "doctor.sh",
                    "commands.yaml",
                    "commands.json",
                    "evidence_map.yaml",
                    "agent_context.md",
                    "failure_playbook.md",
                    "safety_warnings.json",
                },
            )

    def test_bootstrap_writer_clears_existing_bootstrap_directory(self) -> None:
        plan = BootstrapPlan(
            repo_name="fixture",
            install=[CommandCandidate(kind=CommandKind.INSTALL, command="true", maturity_target=Maturity.INSTALLABILITY)],
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="true", maturity_target=Maturity.INSTALLABILITY),
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            old_bootstrap = repo / ".bootstrap"
            old_bootstrap.mkdir()
            (old_bootstrap / "old.txt").write_text("stale\n", encoding="utf-8")

            write_bootstrap(repo, build_manifest(plan))

            self.assertFalse((old_bootstrap / "old.txt").exists())
            self.assertTrue((old_bootstrap / "setup.sh").exists())

    def test_prepare_workspace_no_clone_rejects_url_without_cached_repo(self) -> None:
        with tempfile.TemporaryDirectory() as out_tmp:
            repo = parse_repo_input("https://github.com/example/project")
            with self.assertRaises(FileNotFoundError):
                prepare_workspace(repo, Path(out_tmp) / "repo", allow_clone=False)

    def test_prepare_workspace_preserves_repo_when_source_is_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "workspace" / "repo"
            repo_dir.mkdir(parents=True)
            marker = repo_dir / "pyproject.toml"
            marker.write_text("[project]\nname='fixture'\n", encoding="utf-8")

            prepare_workspace(parse_repo_input(str(repo_dir)), repo_dir, allow_clone=False)

            self.assertTrue(marker.exists())

    def test_prepare_workspace_does_not_copy_generated_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
            (source / ".bootstrap").mkdir()
            (source / ".bootstrap" / "stale.txt").write_text("stale\n", encoding="utf-8")
            destination = root / "run" / "workspace" / "repo"

            prepare_workspace(parse_repo_input(str(source)), destination, allow_clone=False)

            self.assertTrue((destination / "pyproject.toml").exists())
            self.assertFalse((destination / ".bootstrap").exists())

    def test_prepare_workspace_preserves_git_build_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
            (source / ".git").mkdir()
            (source / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (source / "build").mkdir()
            (source / "build" / "build-plugins.mjs").write_text("export {};\n", encoding="utf-8")
            fbcode_builder = source / "build" / "fbcode_builder"
            fbcode_builder.mkdir()
            (fbcode_builder / "getdeps.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            (source / "real.txt").write_text("real\n", encoding="utf-8")
            (source / "link.txt").symlink_to("real.txt")
            destination = root / "run" / "workspace" / "repo"

            prepare_workspace(parse_repo_input(str(source)), destination, allow_clone=False)

            self.assertTrue((destination / ".git" / "HEAD").exists())
            self.assertTrue((destination / "build" / "build-plugins.mjs").exists())
            self.assertTrue((destination / "build" / "fbcode_builder" / "getdeps.py").exists())
            self.assertTrue((destination / "link.txt").is_symlink())
            self.assertEqual((destination / "link.txt").readlink(), Path("real.txt"))

    def test_prepare_workspace_still_ignores_common_generated_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
            for name in ["node_modules", "dist", "target", ".venv", "venv", "__pycache__"]:
                directory = source / name
                directory.mkdir()
                (directory / "generated.txt").write_text("generated\n", encoding="utf-8")
            destination = root / "run" / "workspace" / "repo"

            prepare_workspace(parse_repo_input(str(source)), destination, allow_clone=False)

            self.assertTrue((destination / "pyproject.toml").exists())
            for name in ["node_modules", "dist", "target", ".venv", "venv", "__pycache__"]:
                self.assertFalse((destination / name).exists())

    def test_find_cached_repo_supports_owner_name_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cached = root / "example" / "project"
            cached.mkdir(parents=True)

            self.assertEqual(find_cached_repo("https://github.com/example/project", root), cached)

    def test_run_csv_slice_resolves_url_to_local_repo(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_csv_slice.py"
        spec = importlib.util.spec_from_file_location("run_csv_slice", script_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["run_csv_slice"] = module
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_root = root / "repos"
            local_repo = repo_root / "example_project"
            local_repo.mkdir(parents=True)
            csv_path = root / "projects.csv"
            csv_path.write_text(
                "name,safe_name,url,language\n"
                "example-project,example_project,https://github.com/example/project,Python\n",
                encoding="utf-8",
            )

            jobs = module._load_jobs(csv_path, 1, 1, root / "runs", [repo_root], benchmark=None, allow_clone=False)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].repo, str(local_repo))
            self.assertEqual(jobs[0].language, "Python")

    def test_run_csv_slice_ignores_existing_run_workspace_repo(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_csv_slice.py"
        spec = importlib.util.spec_from_file_location("run_csv_slice", script_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["run_csv_slice"] = module
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_repo = root / "runs" / "example_project" / "workspace" / "repo"
            local_repo.mkdir(parents=True)
            csv_path = root / "projects.csv"
            csv_path.write_text(
                "name,safe_name,url,language\n"
                "example-project,example_project,https://github.com/example/project,Python\n",
                encoding="utf-8",
            )

            with self.assertRaises(FileNotFoundError):
                module._load_jobs(csv_path, 1, 1, root / "runs", [], benchmark=None, allow_clone=False)

    def test_run_csv_slice_clones_missing_url_to_cache(self) -> None:
        script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_csv_slice.py"
        spec = importlib.util.spec_from_file_location("run_csv_slice", script_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["run_csv_slice"] = module
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "benchmark"
            csv_path = root / "projects.csv"
            csv_path.write_text(
                "name,safe_name,url,language\n"
                "example-project,example_project,https://github.com/example/project,Python\n",
                encoding="utf-8",
            )

            with patch.object(module.subprocess, "run") as run:
                run.side_effect = lambda command, check: Path(command[-1]).mkdir(parents=True)
                jobs = module._load_jobs(csv_path, 1, 1, root / "runs", [cache], benchmark=cache, allow_clone=True)

            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].repo, str((cache / "example_project").resolve()))
            run.assert_called_once_with(
                [
                    "git",
                    "clone",
                    "--recurse-submodules",
                    "--depth",
                    "1",
                    "--shallow-submodules",
                    "https://github.com/example/project",
                    str(cache / "example_project"),
                ],
                check=True,
            )

    def test_clone_benchmark_repos_loads_csv_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "projects.csv"
            csv_path.write_text(
                "name,safe_name,url,language\n"
                "example-project,example_project,https://github.com/example/project,Python\n",
                encoding="utf-8",
            )

            repos = _CLONE_BENCHMARK._load_repos(csv_path, root / "benchmark")

            self.assertEqual(len(repos), 1)
            self.assertEqual(repos[0].name, "example_project")
            self.assertEqual(repos[0].target, root / "benchmark" / "example_project")

    def test_clone_benchmark_repos_uses_depth_and_skips_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "benchmark" / "example_project"
            repo = _CLONE_BENCHMARK.Repo(row_number=1, name="example_project", url="https://github.com/example/project", target=target)

            with patch.object(_CLONE_BENCHMARK.subprocess, "run") as run:
                _CLONE_BENCHMARK._clone_or_update(repo, depth=1, update=False, force=False)

            run.assert_called_once_with(
                ["git", "clone", "--recurse-submodules", "--depth", "1", "--shallow-submodules", repo.url, str(target)],
                check=True,
            )

            target.mkdir(parents=True)
            with patch.object(_CLONE_BENCHMARK.subprocess, "run") as run:
                _CLONE_BENCHMARK._clone_or_update(repo, depth=1, update=False, force=False)

            run.assert_not_called()

    def test_clone_benchmark_repos_update_initializes_submodules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "benchmark" / "example_project"
            target.mkdir(parents=True)
            repo = _CLONE_BENCHMARK.Repo(row_number=1, name="example_project", url="https://github.com/example/project", target=target)

            with patch.object(_CLONE_BENCHMARK.subprocess, "run") as run:
                _CLONE_BENCHMARK._clone_or_update(repo, depth=1, update=True, force=False)

            self.assertEqual(run.call_args_list[0].args[0], ["git", "-C", str(target), "fetch", "--all", "--prune"])
            self.assertEqual(
                run.call_args_list[1].args[0],
                ["git", "-C", str(target), "submodule", "update", "--init", "--recursive", "--depth", "1"],
            )

    def test_plan_sanity_rejects_empty_repo_hallucination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
            discovery = discover_repo(repo)
            plan = BootstrapPlan(
                repo_name="fixture",
                doctor=[CommandCandidate(kind=CommandKind.DOCTOR, command="echo 'repo is empty'")],
                minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="python3 --version"),
                agent_context="Repository is empty.",
            )

            with self.assertRaises(PlanSanityError):
                _validate_plan_against_discovery(plan, discovery)

    def test_plan_sanity_rejects_runtime_version_only_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
            discovery = discover_repo(repo)
            plan = BootstrapPlan(
                repo_name="fixture",
                install=[CommandCandidate(kind=CommandKind.INSTALL, command="apt-get update && apt-get install -y python3")],
                minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="python3 --version"),
            )

            with self.assertRaises(PlanSanityError):
                _validate_plan_against_discovery(plan, discovery)

    def test_plan_sanity_rejects_stdlib_only_python_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
            discovery = discover_repo(repo)
            plan = BootstrapPlan(
                repo_name="fixture",
                install=[CommandCandidate(kind=CommandKind.INSTALL, command="python3 -m venv .bootstrap/venv")],
                minimal_verify=CommandCandidate(
                    kind=CommandKind.MINIMAL_VERIFY,
                    command=".bootstrap/venv/bin/python -c \"import sys; print(sys.version)\"",
                ),
            )

            with self.assertRaises(PlanSanityError):
                _validate_plan_against_discovery(plan, discovery)

    def test_doctor_commands_must_be_read_only(self) -> None:
        plan = BootstrapPlan(
            repo_name="fixture",
            doctor=[CommandCandidate(kind=CommandKind.DOCTOR, command="apt-get update && apt-get install -y python3")],
            install=[CommandCandidate(kind=CommandKind.INSTALL, command="apt-get update && apt-get install -y python3", maturity_target=Maturity.INSTALLABILITY)],
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="true", maturity_target=Maturity.INSTALLABILITY),
        )
        manifest = build_manifest(plan)
        warnings = json.loads(manifest.files["safety_warnings.json"])
        self.assertTrue(any(item["category"] == "doctor_mutates_install_environment" for item in warnings))

    def test_install_commands_may_install_dependencies(self) -> None:
        plan = BootstrapPlan(
            repo_name="fixture",
            doctor=[CommandCandidate(kind=CommandKind.DOCTOR, command="python3 --version || true")],
            install=[CommandCandidate(kind=CommandKind.INSTALL, command="apt-get update && apt-get install -y python3", maturity_target=Maturity.INSTALLABILITY)],
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="true", maturity_target=Maturity.INSTALLABILITY),
        )
        manifest = build_manifest(plan)
        self.assertIn("apt-get install", manifest.files["setup.sh"])

    def test_commands_must_not_modify_bootstrap_contract_files(self) -> None:
        plan = BootstrapPlan(
            repo_name="fixture",
            install=[
                CommandCandidate(
                    kind=CommandKind.INSTALL,
                    command="cat > .bootstrap/setup.sh <<'EOF'\ntrue\nEOF",
                    maturity_target=Maturity.INSTALLABILITY,
                )
            ],
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="true", maturity_target=Maturity.INSTALLABILITY),
        )
        manifest = build_manifest(plan)
        warnings = json.loads(manifest.files["safety_warnings.json"])
        self.assertTrue(any(item["category"] == "bootstrap_contract_mutation" for item in warnings))

    def test_public_source_archive_downloads_are_allowed(self) -> None:
        plan = BootstrapPlan(
            repo_name="fixture",
            install=[
                CommandCandidate(
                    kind=CommandKind.INSTALL,
                    command="wget https://archive.apache.org/dist/commons/foo.tar.gz -O /tmp/foo.tgz",
                    maturity_target=Maturity.INSTALLABILITY,
                )
            ],
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="true", maturity_target=Maturity.INSTALLABILITY),
        )
        build_manifest(plan)

    def test_commands_must_not_populate_repo_from_downloaded_source(self) -> None:
        plan = BootstrapPlan(
            repo_name="fixture",
            install=[
                CommandCandidate(
                    kind=CommandKind.INSTALL,
                    command="cp -r /tmp/foo/* /workspace/repo/",
                    maturity_target=Maturity.INSTALLABILITY,
                )
            ],
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="true", maturity_target=Maturity.INSTALLABILITY),
        )
        manifest = build_manifest(plan)
        warnings = json.loads(manifest.files["safety_warnings.json"])
        self.assertTrue(any(item["category"] == "repo_source_mutation" for item in warnings))

    def test_python_dependency_installs_must_use_bootstrap_venv(self) -> None:
        plan = BootstrapPlan(
            repo_name="fixture",
            install=[CommandCandidate(kind=CommandKind.INSTALL, command="pip3 install -e .", maturity_target=Maturity.INSTALLABILITY)],
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="true", maturity_target=Maturity.INSTALLABILITY),
        )
        manifest = build_manifest(plan)
        warnings = json.loads(manifest.files["safety_warnings.json"])
        self.assertTrue(any(item["category"] == "system_python_pip_install" for item in warnings))

    def test_python_dependency_installs_allow_bootstrap_venv_editable(self) -> None:
        plan = BootstrapPlan(
            repo_name="fixture",
            install=[
                CommandCandidate(kind=CommandKind.INSTALL, command="apt-get update && apt-get install -y python3 python3-venv", maturity_target=Maturity.INSTALLABILITY),
                CommandCandidate(kind=CommandKind.INSTALL, command="python3 -m venv .bootstrap/venv", maturity_target=Maturity.INSTALLABILITY),
                CommandCandidate(kind=CommandKind.INSTALL, command=".bootstrap/venv/bin/python -m pip install -e .", maturity_target=Maturity.INSTALLABILITY),
            ],
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command=".bootstrap/venv/bin/python -c 'print(1)'", maturity_target=Maturity.INSTALLABILITY),
        )
        manifest = build_manifest(plan)
        self.assertIn(".bootstrap/venv/bin/python -m pip install -e .", manifest.files["setup.sh"])

    def test_doctor_commands_are_not_silenced_by_default(self) -> None:
        plan = BootstrapPlan(
            repo_name="fixture",
            doctor=[CommandCandidate(kind=CommandKind.DOCTOR, command=".bootstrap/venv/bin/python -c 'import fixture'")],
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="true", maturity_target=Maturity.INSTALLABILITY),
        )
        manifest = build_manifest(plan)
        self.assertIn(".bootstrap/venv/bin/python -c 'import fixture'\n", manifest.files["doctor.sh"])
        self.assertNotIn("|| true", manifest.files["doctor.sh"])

    def test_python_dependency_installs_allow_bootstrap_venv(self) -> None:
        plan = BootstrapPlan(
            repo_name="fixture",
            install=[
                CommandCandidate(kind=CommandKind.INSTALL, command="apt-get update && apt-get install -y python3 python3-venv", maturity_target=Maturity.INSTALLABILITY),
                CommandCandidate(kind=CommandKind.INSTALL, command="python3 -m venv .bootstrap/venv", maturity_target=Maturity.INSTALLABILITY),
                CommandCandidate(kind=CommandKind.INSTALL, command=".bootstrap/venv/bin/python -m pip install .", maturity_target=Maturity.INSTALLABILITY),
            ],
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command=".bootstrap/venv/bin/python -c 'print(1)'", maturity_target=Maturity.INSTALLABILITY),
        )
        manifest = build_manifest(plan)
        self.assertIn(".bootstrap/venv/bin/python -m pip install .", manifest.files["setup.sh"])

    def test_orchestrator_generates_without_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as out_tmp:
            repo = Path(repo_tmp)
            (repo / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0.1.0'\n", encoding="utf-8")
            (repo / "fixture.py").write_text("x = 1\n", encoding="utf-8")
            log = BootstrapOrchestrator(allow_fallback=True).bootstrap(str(repo), Path(out_tmp), verify=False)
            self.assertEqual(log.status, "success")
            self.assertTrue((Path(out_tmp) / "workspace" / "repo" / ".bootstrap" / "verify.sh").exists())
            setup = (Path(out_tmp) / "workspace" / "repo" / ".bootstrap" / "setup.sh").read_text(encoding="utf-8")
            self.assertIn("apt-get update && apt-get install -y python3 python3-venv python3-pip python3-dev build-essential pkg-config", setup)
            self.assertLess(setup.index("apt-get update"), setup.index("python3 -m venv"))
            self.assertTrue((Path(out_tmp) / "agent_outputs" / "llm_status.json").exists())
            events_path = Path(out_tmp) / "agent_outputs" / "llm_events.jsonl"
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[0]["status"], "running")
            self.assertEqual(events[-2]["status"], "failed")
            self.assertEqual(events[-1]["status"], "fallback")
            self.assertIn("elapsed_sec", events[-1])

    def test_initial_llm_structured_failure_retries_once(self) -> None:
        class FlakyClient:
            calls = 0

            def generate_bootstrap_plan(self, discovery, ci) -> BootstrapPlan:
                self.calls += 1
                if self.calls == 1:
                    raise ValueError("empty structured response")
                return BootstrapPlan(
                    repo_name=discovery.repo_name,
                    install=[CommandCandidate(kind=CommandKind.INSTALL, command="true", maturity_target=Maturity.INSTALLABILITY)],
                    minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="true", maturity_target=Maturity.INSTALLABILITY),
                )

        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as out_tmp:
            repo = Path(repo_tmp)
            (repo / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0.1.0'\n", encoding="utf-8")
            (repo / "fixture.py").write_text("x = 1\n", encoding="utf-8")
            client = FlakyClient()
            orchestrator = BootstrapOrchestrator(allow_fallback=False)
            orchestrator._llm_client = lambda: client  # type: ignore[method-assign]

            log = orchestrator.bootstrap(str(repo), Path(out_tmp), verify=False)

            self.assertEqual(log.status, "success")
            self.assertEqual(client.calls, 2)
            events = [json.loads(line) for line in (Path(out_tmp) / "agent_outputs" / "llm_events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([event["status"] for event in events], ["running", "retrying", "running", "success"])

    def test_evaluation_log_records_llm_token_usage(self) -> None:
        class UsageClient:
            last_usage = None

            def generate_bootstrap_plan(self, discovery, ci) -> BootstrapPlan:
                self.last_usage = {
                    "input_tokens": 100,
                    "output_tokens": 25,
                    "total_tokens": 125,
                    "estimated": False,
                }
                return BootstrapPlan(
                    repo_name=discovery.repo_name,
                    install=[CommandCandidate(kind=CommandKind.INSTALL, command="true", maturity_target=Maturity.INSTALLABILITY)],
                    minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="true", maturity_target=Maturity.INSTALLABILITY),
                )

        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as out_tmp:
            repo = Path(repo_tmp)
            (repo / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0.1.0'\n", encoding="utf-8")
            (repo / "fixture.py").write_text("x = 1\n", encoding="utf-8")
            client = UsageClient()
            orchestrator = BootstrapOrchestrator(allow_fallback=False)
            orchestrator.llm = client  # type: ignore[assignment]
            orchestrator._llm_client = lambda: client  # type: ignore[method-assign]

            log = orchestrator.bootstrap(str(repo), Path(out_tmp), verify=False)

            self.assertEqual(log.token_cost, 125.0)
            self.assertEqual(log.metadata["llm_usage_total"]["total_tokens"], 125)
            events = [json.loads(line) for line in (Path(out_tmp) / "agent_outputs" / "llm_events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["llm_usage"]["total_tokens"], 125)

    def test_repair_llm_structured_failure_retries_twice(self) -> None:
        with tempfile.TemporaryDirectory() as out_tmp:
            paths = RunPaths.create(Path(out_tmp))
            paths.ensure()
            orchestrator = BootstrapOrchestrator()
            calls = 0

            def flaky_call() -> str:
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise ValueError("empty structured response")
                return "ok"

            result = orchestrator._invoke_llm_with_retries(paths, "repair_plan", flaky_call, round=1)

            self.assertEqual(result, "ok")
            self.assertEqual(calls, 3)
            events = [json.loads(line) for line in (Path(out_tmp) / "agent_outputs" / "llm_events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([event["status"] for event in events], ["running", "retrying", "running", "retrying", "running", "success"])

    def test_repair_llm_timeout_retries_then_fails(self) -> None:
        with tempfile.TemporaryDirectory() as out_tmp:
            paths = RunPaths.create(Path(out_tmp))
            paths.ensure()
            config = RuntimeConfig()
            config.budget.max_repair_llm_structured_retries = 1
            config.budget.llm_repair_timeout_sec = 1
            orchestrator = BootstrapOrchestrator(config=config)

            def slow_call() -> str:
                time.sleep(5)
                return "late"

            with self.assertRaisesRegex(TimeoutError, "LLM repair_plan timed out after 1s"):
                orchestrator._invoke_llm_with_retries(paths, "repair_plan", slow_call, round=1)

            events = [json.loads(line) for line in (Path(out_tmp) / "agent_outputs" / "llm_events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([event["status"] for event in events], ["running", "retrying", "running", "failed"])
            self.assertEqual(events[-1]["error_type"], "TimeoutError")

    def test_orchestrator_propagates_phase_timeout_to_llm_client(self) -> None:
        with tempfile.TemporaryDirectory() as out_tmp:
            paths = RunPaths.create(Path(out_tmp))
            paths.ensure()
            config = RuntimeConfig()
            config.budget.max_repair_llm_structured_retries = 0
            config.budget.llm_repair_timeout_sec = 7
            config.budget.llm_initial_timeout_sec = 11
            orchestrator = BootstrapOrchestrator(config=config)

            class StubLLM:
                def __init__(self) -> None:
                    self.timeouts: list[int | None] = []

                def configure_request_timeout(self, timeout_sec: int | None) -> None:
                    self.timeouts.append(timeout_sec)

                def configure_conversation_logging(self, *_a, **_kw) -> None:
                    pass

            stub = StubLLM()
            orchestrator.llm = stub  # type: ignore[assignment]

            orchestrator._invoke_llm_with_retries(paths, "initial_plan", lambda: orchestrator._llm_client())
            orchestrator._invoke_llm_with_retries(paths, "repair_plan", lambda: orchestrator._llm_client(), round=1)

            self.assertEqual(stub.timeouts, [11, 7])

    def test_empty_structured_response_has_clear_error(self) -> None:
        client = object.__new__(DeepAgentsStructuredClient)

        def create_agent(**kwargs):
            class EmptyAgent:
                def invoke(self, payload):
                    return {"structured_response": ""}

            return EmptyAgent()

        client._create_deep_agent = create_agent  # type: ignore[attr-defined]
        client.model = "test:model"  # type: ignore[attr-defined]

        with self.assertRaisesRegex(ValueError, "empty structured response for BootstrapPlan"):
            client._invoke_structured(BootstrapPlan, "system", "prompt")  # type: ignore[attr-defined]

    def test_request_timeout_is_passed_to_chat_model_constructor(self) -> None:
        captured: dict[str, object] = {}

        class FakeChatDeepSeek:
            def __init__(self, *, model: str, timeout: float) -> None:
                captured["provider"] = "deepseek"
                captured["model"] = model
                captured["timeout"] = timeout

        fake_module = type(sys)("langchain_deepseek")
        fake_module.ChatDeepSeek = FakeChatDeepSeek  # type: ignore[attr-defined]

        agents_seen: dict[str, object] = {}

        def create_agent(**kwargs):
            agents_seen["model"] = kwargs.get("model")

            class EchoAgent:
                def invoke(self, payload):
                    plan = BootstrapPlan(
                        repo_name="fixture",
                        doctor=[],
                        install=[],
                        minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="true", confidence=0.5, timeout_sec=10),
                    )
                    return {"structured_response": plan}

            return EchoAgent()

        client = object.__new__(DeepAgentsStructuredClient)
        client.model = "deepseek:deepseek-chat"  # type: ignore[attr-defined]
        client.last_usage = None  # type: ignore[attr-defined]
        client.conversation_log_dir = None  # type: ignore[attr-defined]
        client.conversation_log_context = {}  # type: ignore[attr-defined]
        client.request_timeout_sec = 42  # type: ignore[attr-defined]
        client._create_deep_agent = create_agent  # type: ignore[attr-defined]

        with patch.dict(sys.modules, {"langchain_deepseek": fake_module}):
            client._invoke_structured(BootstrapPlan, "system", "prompt")  # type: ignore[attr-defined]

        self.assertEqual(captured["provider"], "deepseek")
        self.assertEqual(captured["model"], "deepseek-chat")
        self.assertEqual(captured["timeout"], 42)
        self.assertIsInstance(agents_seen["model"], FakeChatDeepSeek)

    def test_request_timeout_unset_passes_model_string_through(self) -> None:
        agents_seen: dict[str, object] = {}

        def create_agent(**kwargs):
            agents_seen["model"] = kwargs.get("model")

            class EchoAgent:
                def invoke(self, payload):
                    plan = BootstrapPlan(
                        repo_name="fixture",
                        doctor=[],
                        install=[],
                        minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="true", confidence=0.5, timeout_sec=10),
                    )
                    return {"structured_response": plan}

            return EchoAgent()

        client = object.__new__(DeepAgentsStructuredClient)
        client.model = "deepseek:deepseek-chat"  # type: ignore[attr-defined]
        client.last_usage = None  # type: ignore[attr-defined]
        client.conversation_log_dir = None  # type: ignore[attr-defined]
        client.conversation_log_context = {}  # type: ignore[attr-defined]
        client.request_timeout_sec = None  # type: ignore[attr-defined]
        client._create_deep_agent = create_agent  # type: ignore[attr-defined]

        client._invoke_structured(BootstrapPlan, "system", "prompt")  # type: ignore[attr-defined]

        self.assertEqual(agents_seen["model"], "deepseek:deepseek-chat")

    def test_llm_conversation_logging_records_prompt_and_raw_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = object.__new__(DeepAgentsStructuredClient)

            def create_agent(**kwargs):
                class EmptyAgent:
                    def invoke(self, payload):
                        return {"structured_response": "", "messages": [{"role": "assistant", "content": ""}]}

                return EmptyAgent()

            client._create_deep_agent = create_agent  # type: ignore[attr-defined]
            client.model = "test:model"  # type: ignore[attr-defined]
            client.last_usage = None  # type: ignore[attr-defined]
            client.configure_conversation_logging(Path(tmp), phase="repair_plan", round=2, attempt=3)  # type: ignore[attr-defined]

            with self.assertRaisesRegex(ValueError, "empty structured response for BootstrapPlan"):
                client._invoke_structured(BootstrapPlan, "system prompt", "user prompt")  # type: ignore[attr-defined]

            logs = list(Path(tmp).glob("repair_plan_round_2_attempt_3_BootstrapPlan.json"))
            self.assertEqual(len(logs), 1)
            record = json.loads(logs[0].read_text(encoding="utf-8"))
            self.assertEqual(record["system_prompt"], "system prompt")
            self.assertEqual(record["request"]["messages"][0]["content"], "user prompt")
            self.assertEqual(record["result"]["structured_response"], "")
            self.assertEqual(record["context"]["phase"], "repair_plan")

    def test_llm_repair_plan_uses_delta_schema_and_merges_locally(self) -> None:
        captured: dict[str, object] = {}

        def create_agent(**kwargs):
            captured["tools"] = kwargs.get("tools")
            captured["response_format"] = kwargs.get("response_format")
            captured["system_prompt"] = kwargs.get("system_prompt")

            class DeltaAgent:
                def invoke(self, payload):
                    captured["prompt"] = payload["messages"][0]["content"]
                    return {
                        "structured_response": RepairPlanDelta(
                            diagnosis="meson is not on PATH inside the build subprocess",
                            replace_commands=[
                                {
                                    "section": "install",
                                    "index": 0,
                                    "command": CommandCandidate(
                                        kind=CommandKind.INSTALL,
                                        command="PATH=/workspace/repo/.bootstrap/venv/bin:$PATH .bootstrap/venv/bin/python -m pip install --no-build-isolation -e .",
                                        source="repair",
                                        maturity_target=Maturity.TESTABILITY,
                                        reason="Set the venv bin directory on PATH so meson-python can find meson during editable metadata generation",
                                    ),
                                }
                            ],
                            failure_playbook_append="If meson is not found, set the venv bin directory on PATH for the install command.",
                            commands_added_or_removed=["replaced install[0]"],
                        )
                    }

            return DeltaAgent()

        client = object.__new__(DeepAgentsStructuredClient)
        client.model = "test:model"  # type: ignore[attr-defined]
        client.last_usage = None  # type: ignore[attr-defined]
        client.conversation_log_dir = None  # type: ignore[attr-defined]
        client.conversation_log_context = {}  # type: ignore[attr-defined]
        client.request_timeout_sec = None  # type: ignore[attr-defined]
        client._create_deep_agent = create_agent  # type: ignore[attr-defined]

        plan = BootstrapPlan(
            repo_name="fixture",
            install=[CommandCandidate(kind=CommandKind.INSTALL, command=".bootstrap/venv/bin/python -m pip install --no-build-isolation -e .")],
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command=".bootstrap/venv/bin/python -c 'import fixture'"),
        )
        verifier_result = VerifierResult(
            status="fail",
            stop_reason=StopReason.VERIFIER_FAILED,
            traces=[CommandTrace(command="bash .bootstrap/setup.sh", cwd="/workspace/repo", exit_code=1, elapsed_sec=1.0, stdout_summary='meson-python: error: meson executable "meson" not found')],
        )

        repair = client.repair_plan(plan, verifier_result)  # type: ignore[attr-defined]

        self.assertEqual(captured["tools"], [])
        self.assertIs(captured["response_format"], RepairPlanDelta)
        self.assertIn("Do not call tools", captured["system_prompt"])
        self.assertIn("Return only the smallest delta", captured["prompt"])
        self.assertIn("PATH=/workspace/repo/.bootstrap/venv/bin:$PATH", repair.plan.install[0].command)
        self.assertEqual(repair.plan.minimal_verify.command, plan.minimal_verify.command)
        self.assertIn("meson is not found", repair.plan.failure_playbook)
        self.assertEqual(repair.commands_added_or_removed, ["replaced install[0]"])

    def test_compact_bootstrap_plan_includes_command_indices(self) -> None:
        plan = BootstrapPlan(
            repo_name="fixture",
            doctor=[CommandCandidate(kind=CommandKind.DOCTOR, command="true")],
            install=[
                CommandCandidate(kind=CommandKind.INSTALL, command="first"),
                CommandCandidate(kind=CommandKind.INSTALL, command="second"),
            ],
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="true"),
        )

        compact = _compact_bootstrap_plan(plan)

        self.assertEqual(compact["doctor"][0]["index"], 0)
        self.assertEqual(compact["install"][0]["index"], 0)
        self.assertEqual(compact["install"][1]["index"], 1)

    def test_llm_repair_delta_can_move_list_commands(self) -> None:
        captured: dict[str, object] = {}

        def create_agent(**kwargs):
            class MoveAgent:
                def invoke(self, payload):
                    captured["prompt"] = payload["messages"][0]["content"]
                    return {
                        "structured_response": RepairPlanDelta(
                            diagnosis="command order is wrong",
                            move_commands=[{"section": "install", "from_index": 2, "to_index": 1}],
                        )
                    }

            return MoveAgent()

        client = object.__new__(DeepAgentsStructuredClient)
        client.model = "test:model"  # type: ignore[attr-defined]
        client.last_usage = None  # type: ignore[attr-defined]
        client.conversation_log_dir = None  # type: ignore[attr-defined]
        client.conversation_log_context = {}  # type: ignore[attr-defined]
        client.request_timeout_sec = None  # type: ignore[attr-defined]
        client._create_deep_agent = create_agent  # type: ignore[attr-defined]

        plan = BootstrapPlan(
            repo_name="fixture",
            install=[
                CommandCandidate(kind=CommandKind.INSTALL, command="setup runtime"),
                CommandCandidate(kind=CommandKind.INSTALL, command="install project"),
                CommandCandidate(kind=CommandKind.INSTALL, command="install missing build tool"),
            ],
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="true"),
        )

        repair = client.repair_plan(plan, VerifierResult(status="fail", stop_reason=StopReason.VERIFIER_FAILED))

        self.assertIn("move_commands for reordering", captured["prompt"])
        self.assertEqual([command.command for command in repair.plan.install], ["setup runtime", "install missing build tool", "install project"])

    def test_compact_verifier_result_truncates_failure_logs(self) -> None:
        long_output = "x" * 7000
        result = VerifierResult(
            status="fail",
            stop_reason=StopReason.VERIFIER_FAILED,
            traces=[
                CommandTrace(command="bash .bootstrap/setup.sh", cwd="/workspace/repo", exit_code=1, elapsed_sec=0.1, stdout_summary=long_output),
            ],
        )

        compact = _compact_verifier_result(result)

        self.assertEqual(compact["failed_trace"]["command"], "bash .bootstrap/setup.sh")
        self.assertLess(len(compact["failed_trace"]["stdout_summary"]), len(long_output))
        self.assertIn("truncated", compact["failed_trace"]["stdout_summary"])

    def test_compact_verifier_result_hints_source_tree_shadowing(self) -> None:
        result = VerifierResult(
            status="fail",
            stop_reason=StopReason.VERIFIER_FAILED,
            traces=[
                CommandTrace(command="bash .bootstrap/setup.sh", cwd="/workspace/repo", exit_code=0, elapsed_sec=1.0),
                CommandTrace(
                    command="bash .bootstrap/verify.sh",
                    cwd="/workspace/repo",
                    exit_code=1,
                    elapsed_sec=0.1,
                    stdout_summary=(
                        "Traceback (most recent call last):\n"
                        "  File \"<string>\", line 1, in <module>\n"
                        "  File \"/workspace/repo/pandas/__init__.py\", line 44, in <module>\n"
                        "    import pandas.core.config_init\n"
                        "ModuleNotFoundError: No module named 'pandas._libs.pandas_parser'\n"
                    ),
                ),
            ],
        )

        compact = _compact_verifier_result(result)

        self.assertTrue(any("source-tree shadowing" in hint for hint in compact["repair_hints"]))
        self.assertTrue(any("cd /tmp" in hint for hint in compact["repair_hints"]))

    def test_python_policy_allows_container_path_for_shadowing_repairs(self) -> None:
        self.assertIn("source-tree shadowing", _BOOTSTRAP_COMMAND_CONSTRAINTS)
        self.assertIn("/workspace/repo", _BOOTSTRAP_COMMAND_CONSTRAINTS)
        self.assertIn("cd /tmp", _BOOTSTRAP_COMMAND_CONSTRAINTS)

    def test_prompt_requires_reason_command_consistency(self) -> None:
        self.assertIn("reason` must match the actual `command`", _BOOTSTRAP_COMMAND_CONSTRAINTS)
        self.assertIn("PATH=/workspace/repo/.bootstrap/venv/bin:$PATH", _BOOTSTRAP_COMMAND_CONSTRAINTS)
        self.assertIn("If the reason says PATH is set, the command must set PATH", _BOOTSTRAP_COMMAND_CONSTRAINTS)

    def test_prompt_warns_about_commands_rewriting_bootstrap_scripts(self) -> None:
        self.assertIn("Prefer commands that do not rewrite these generated contract files", _BOOTSTRAP_COMMAND_CONSTRAINTS)
        self.assertIn("The system writes `.bootstrap/setup.sh`, `.bootstrap/doctor.sh`, `.bootstrap/verify.sh`, and command metadata files", _BOOTSTRAP_COMMAND_CONSTRAINTS)
        self.assertIn(".bootstrap/safety_warnings.json", _BOOTSTRAP_COMMAND_CONSTRAINTS)
        self.assertIn("Repository mutations during setup are allowed", _BOOTSTRAP_COMMAND_CONSTRAINTS)

    def test_prompt_requires_python_runtime_before_venv(self) -> None:
        self.assertIn("do not assume the Docker image already has Python", _BOOTSTRAP_COMMAND_CONSTRAINTS)
        self.assertIn("apt-get update && apt-get install -y python3 python3-venv python3-pip python3-dev build-essential pkg-config", _BOOTSTRAP_COMMAND_CONSTRAINTS)
        self.assertIn("do not switch to bare `python`", _BOOTSTRAP_COMMAND_CONSTRAINTS)

    def test_prompt_assumes_minimal_fresh_container(self) -> None:
        self.assertIn("fresh, minimal Ubuntu container", _BOOTSTRAP_COMMAND_CONSTRAINTS)
        self.assertIn("install or enable it in `install` commands before first use", _BOOTSTRAP_COMMAND_CONSTRAINTS)
        self.assertIn("Do not assume Node.js, npm, yarn, pnpm, or corepack already exist", _BOOTSTRAP_COMMAND_CONSTRAINTS)
        self.assertIn("Do not assume compilers, CMake, Ninja, Meson, pkg-config, autotools", _BOOTSTRAP_COMMAND_CONSTRAINTS)

    def test_prompt_rejects_vacuous_non_empty_repo_validation(self) -> None:
        self.assertIn("do not claim the repository is empty", _BOOTSTRAP_COMMAND_CONSTRAINTS)
        self.assertIn("Do not use a runtime version check", _BOOTSTRAP_COMMAND_CONSTRAINTS)

    def test_prompt_profiles_select_bazel_without_python_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "MODULE.bazel").write_text("module(name = 'fixture')\n", encoding="utf-8")
            discovery = discover_repo(repo)

            constraints = bootstrap_command_constraints(discovery)

            self.assertIn("Bazel / C/C++ project profile", constraints)
            self.assertIn("Do not replace a Bazel/C/C++ source repository bootstrap with Python runtime checks", constraints)
            self.assertNotIn("Python bootstrap policy", constraints)

    def test_prompt_profiles_select_python_policy_for_python_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
            discovery = discover_repo(repo)

            constraints = bootstrap_command_constraints(discovery)

            self.assertIn("Python project profile", constraints)
            self.assertIn("Python bootstrap policy", constraints)
            self.assertNotIn("Bazel / C/C++ project profile", constraints)

    def test_prompt_profiles_do_not_select_python_for_helper_script_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "package.json").write_text('{"scripts":{"test":"node test.js"}}\n', encoding="utf-8")
            (repo / "scripts").mkdir()
            (repo / "scripts" / "helper.py").write_text("print('helper')\n", encoding="utf-8")
            discovery = discover_repo(repo)

            constraints = bootstrap_command_constraints(discovery)

            self.assertIn("Node / JavaScript project profile", constraints)
            self.assertNotIn("Python bootstrap policy", constraints)

    def test_prompt_profiles_select_rust_and_go_policies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "Cargo.toml").write_text("[package]\nname='fixture'\nversion='0.1.0'\n", encoding="utf-8")
            constraints = bootstrap_command_constraints(discover_repo(repo))
            self.assertIn("Rust project profile", constraints)
            self.assertIn("cargo check", constraints)

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "go.mod").write_text("module example.com/fixture\n", encoding="utf-8")
            constraints = bootstrap_command_constraints(discover_repo(repo))
            self.assertIn("Go project profile", constraints)
            self.assertIn("go test ./...", constraints)

    def test_repair_prompt_profiles_infer_bazel_from_plan_evidence(self) -> None:
        plan = BootstrapPlan(
            repo_name="fixture",
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="test -f MODULE.bazel"),
            evidence=[
                {
                    "source": "package_metadata",
                    "path": "MODULE.bazel",
                    "summary": "Bazel module metadata",
                }
            ],
        )

        constraints = repair_command_constraints(plan)

        self.assertIn("Bazel / C/C++ project profile", constraints)
        self.assertNotIn("Python bootstrap policy", constraints)

    def test_repair_prompt_does_not_infer_python_from_helper_mentions(self) -> None:
        plan = BootstrapPlan(
            repo_name="fixture",
            install=[CommandCandidate(kind=CommandKind.INSTALL, command="npm install")],
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="npm test"),
            evidence=[
                {
                    "source": "docs",
                    "path": "scripts/helper.py",
                    "summary": "A Python helper script exists for docs generation",
                },
                {
                    "source": "package_metadata",
                    "path": "package.json",
                    "summary": "Node package metadata",
                },
            ],
        )

        constraints = repair_command_constraints(plan)

        self.assertIn("Node / JavaScript project profile", constraints)
        self.assertNotIn("Python bootstrap policy", constraints)

    def test_orchestrator_clears_stale_run_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as out_tmp:
            repo = Path(repo_tmp)
            out = Path(out_tmp)
            (repo / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0.1.0'\n", encoding="utf-8")
            (repo / "fixture.py").write_text("x = 1\n", encoding="utf-8")
            (out / "agent_outputs").mkdir()
            (out / "traces").mkdir()
            (out / "agent_outputs" / "stale.json").write_text("{}\n", encoding="utf-8")
            (out / "traces" / "stale.json").write_text("{}\n", encoding="utf-8")
            (out / "evaluation_log.json").write_text("{}\n", encoding="utf-8")

            log = BootstrapOrchestrator(allow_fallback=True).bootstrap(str(repo), out, verify=False)

            self.assertEqual(log.status, "success")
            self.assertFalse((out / "agent_outputs" / "stale.json").exists())
            self.assertFalse((out / "traces" / "stale.json").exists())

    def test_command_count_survives_repair_failure(self) -> None:
        class FailingVerifier:
            def verify(self, repo_dir: Path, log_dir: Path | None = None, round_name: str = "verifier") -> VerifierResult:
                return VerifierResult(
                    status="fail",
                    stop_reason=StopReason.VERIFIER_FAILED,
                    traces=[
                        CommandTrace(
                            command="bash .bootstrap/setup.sh",
                            cwd="/workspace/repo",
                            exit_code=1,
                            elapsed_sec=0.1,
                        )
                    ],
                )

        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as out_tmp:
            repo = Path(repo_tmp)
            (repo / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0.1.0'\n", encoding="utf-8")
            (repo / "fixture.py").write_text("x = 1\n", encoding="utf-8")
            orchestrator = BootstrapOrchestrator(allow_fallback=True)
            orchestrator.verifier = FailingVerifier()  # type: ignore[assignment]
            orchestrator._repair_plan = lambda plan, verifier_result, paths, round: (_ for _ in ()).throw(RuntimeError("empty repair response"))  # type: ignore[method-assign]

            log = orchestrator.bootstrap(str(repo), Path(out_tmp), verify=True)

            self.assertEqual(log.status, "fail")
            self.assertEqual(log.command_count, 1)
            self.assertEqual(len(log.trace_files), 1)

    def test_repair_sanity_failure_preserves_verifier_stop_reason(self) -> None:
        class FailingVerifier:
            def verify(self, repo_dir: Path, log_dir: Path | None = None, round_name: str = "verifier") -> VerifierResult:
                return VerifierResult(
                    status="fail",
                    stop_reason=StopReason.COMMAND_TIMEOUT,
                    traces=[
                        CommandTrace(
                            command="bash .bootstrap/setup.sh",
                            cwd="/workspace/repo",
                            exit_code=None,
                            elapsed_sec=600.0,
                            timeout=True,
                        )
                    ],
                )

        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as out_tmp:
            repo = Path(repo_tmp)
            (repo / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0.1.0'\n", encoding="utf-8")
            (repo / "fixture.py").write_text("x = 1\n", encoding="utf-8")
            orchestrator = BootstrapOrchestrator(allow_fallback=True)
            orchestrator.verifier = FailingVerifier()  # type: ignore[assignment]
            orchestrator._repair_plan = lambda plan, verifier_result, paths, round: (_ for _ in ()).throw(PlanSanityError("vacuous repair plan"))  # type: ignore[method-assign]

            log = orchestrator.bootstrap(str(repo), Path(out_tmp), verify=True)

            self.assertEqual(log.status, "fail")
            self.assertEqual(log.stop_reason, StopReason.COMMAND_TIMEOUT)
            self.assertTrue(log.metadata["repair_plan_rejected_by_sanity_check"])
            self.assertEqual(log.metadata["underlying_verifier_stop_reason"], "command_timeout")

    def test_repair_rejects_strongest_test_downgrade(self) -> None:
        plan = BootstrapPlan(
            repo_name="fixture",
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="python -c 'import fixture'", maturity_target=Maturity.TESTABILITY),
            strongest_verify=CommandCandidate(kind=CommandKind.STRONGEST_VERIFY, command="test -f pyproject.toml", maturity_target=Maturity.INSTALLABILITY),
        )

        with self.assertRaises(PlanSanityError):
            _validate_strongest_not_downgraded(plan, 50)

    def test_repair_preserves_previous_strongest_when_repair_downgrades_it(self) -> None:
        initial_plan = BootstrapPlan(
            repo_name="fixture",
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="python -c 'import missing_fixture'", maturity_target=Maturity.INSTALLABILITY),
            strongest_verify=CommandCandidate(kind=CommandKind.STRONGEST_VERIFY, command="python -m pytest", maturity_target=Maturity.TESTABILITY),
        )
        repaired_plan = initial_plan.model_copy(
            deep=True,
            update={
                "minimal_verify": CommandCandidate(
                    kind=CommandKind.MINIMAL_VERIFY,
                    command="python -c 'import fixture'",
                    maturity_target=Maturity.INSTALLABILITY,
                ),
                "strongest_verify": CommandCandidate(
                    kind=CommandKind.STRONGEST_VERIFY,
                    command="test -f pyproject.toml",
                    maturity_target=Maturity.INSTALLABILITY,
                ),
            },
        )

        class FailingThenPassingVerifier:
            def __init__(self) -> None:
                self.calls = 0

            def verify(self, repo_dir: Path, log_dir: Path | None = None, round_name: str = "verifier") -> VerifierResult:
                self.calls += 1
                if self.calls == 1:
                    return VerifierResult(
                        status="fail",
                        stop_reason=StopReason.VERIFIER_FAILED,
                        maturity_reached=Maturity.INSTALLABILITY,
                        failed_stage="minimal_verify",
                        minimal_passed=False,
                        strongest_passed=False,
                        traces=[
                            CommandTrace(command="python -c 'import missing_fixture'", cwd="/workspace/repo", exit_code=1, elapsed_sec=0.1, stage="minimal_verify"),
                        ],
                    )
                return VerifierResult(
                    status="success",
                    stop_reason=StopReason.SUCCESS,
                    maturity_reached=Maturity.TESTABILITY,
                    minimal_passed=True,
                    strongest_passed=True,
                    traces=[
                        CommandTrace(command="python -c 'import fixture'", cwd="/workspace/repo", exit_code=0, elapsed_sec=0.1, stage="minimal_verify"),
                        CommandTrace(command="python -m pytest", cwd="/workspace/repo", exit_code=0, elapsed_sec=0.1, stage="strongest_verify"),
                    ],
                )

        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as out_tmp:
            repo = Path(repo_tmp)
            (repo / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0.1.0'\n", encoding="utf-8")
            (repo / "fixture.py").write_text("x = 1\n", encoding="utf-8")
            orchestrator = BootstrapOrchestrator(allow_fallback=True)
            verifier = FailingThenPassingVerifier()
            orchestrator.verifier = verifier  # type: ignore[assignment]
            orchestrator._initial_plan = lambda repo_input, paths, agent_files: initial_plan  # type: ignore[method-assign]
            orchestrator._repair_plan = lambda plan, verifier_result, paths, round: RepairPlan(diagnosis="fix minimal", plan=repaired_plan)  # type: ignore[method-assign]

            log = orchestrator.bootstrap(str(repo), Path(out_tmp), verify=True)

            self.assertEqual(log.status, "success")
            self.assertEqual(log.retry_count, 1)
            self.assertEqual(log.minimal_command, "python -c 'import fixture'")
            self.assertEqual(log.strongest_local_ci_command, "python -m pytest")
            self.assertEqual(verifier.calls, 2)

    def test_repair_rejects_strongest_test_failure_masking(self) -> None:
        plan = BootstrapPlan(
            repo_name="fixture",
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="python -c 'import fixture'", maturity_target=Maturity.TESTABILITY),
            strongest_verify=CommandCandidate(
                kind=CommandKind.STRONGEST_VERIFY,
                command="mvn test --batch-mode -Dmaven.test.failure.ignore=true",
                maturity_target=Maturity.TESTABILITY,
            ),
        )

        with self.assertRaises(PlanSanityError):
            _validate_strongest_not_downgraded(plan, 50)

    def test_strongest_advisory_failure_does_not_repair_or_fail(self) -> None:
        plan = BootstrapPlan(
            repo_name="fixture",
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="python -c 'import fixture'", maturity_target=Maturity.TESTABILITY),
            strongest_verify=CommandCandidate(kind=CommandKind.STRONGEST_VERIFY, command="python -m pytest", maturity_target=Maturity.TESTABILITY),
        )

        class StrongestFailingVerifier:
            def verify(self, repo_dir: Path, log_dir: Path | None = None, round_name: str = "verifier") -> VerifierResult:
                return VerifierResult(
                    status="success",
                    stop_reason=StopReason.SUCCESS,
                    maturity_reached=Maturity.TESTABILITY,
                    failed_stage=None,
                    minimal_passed=True,
                    strongest_passed=False,
                    stage_results=[
                        StageResult(stage="minimal_verify", status="success", command="python -c 'import fixture'", exit_code=0, maturity_target=Maturity.TESTABILITY),
                        StageResult(stage="strongest_verify", status="fail", command="python -m pytest", exit_code=1, maturity_target=Maturity.TESTABILITY, failure_type="test_failure"),
                    ],
                    traces=[
                        CommandTrace(command="bash .bootstrap/setup.sh", cwd="/workspace/repo", exit_code=0, elapsed_sec=0.1, stage="setup"),
                        CommandTrace(command="bash .bootstrap/doctor.sh", cwd="/workspace/repo", exit_code=0, elapsed_sec=0.1, stage="doctor"),
                        CommandTrace(command="python -c 'import fixture'", cwd="/workspace/repo", exit_code=0, elapsed_sec=0.1, stage="minimal_verify"),
                        CommandTrace(
                            command="python -m pytest",
                            cwd="/workspace/repo",
                            exit_code=1,
                            elapsed_sec=0.1,
                            stage="strongest_verify",
                            stderr_summary="FAILED tests/test_fixture.py::test_value - AssertionError: expected 1",
                        ),
                    ],
                )

        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as out_tmp:
            repo = Path(repo_tmp)
            (repo / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0.1.0'\n", encoding="utf-8")
            (repo / "fixture.py").write_text("x = 1\n", encoding="utf-8")
            orchestrator = BootstrapOrchestrator(allow_fallback=True)
            orchestrator._initial_plan = lambda repo_input, paths, agent_files: plan  # type: ignore[method-assign]
            orchestrator.verifier = StrongestFailingVerifier()  # type: ignore[assignment]
            repair_calls = []

            def repair_same(current_plan, verifier_result, paths, round):
                repair_calls.append(round)
                return RepairPlan(diagnosis="still failing strongest test", plan=current_plan)

            orchestrator._repair_plan = repair_same  # type: ignore[method-assign]

            log = orchestrator.bootstrap(str(repo), Path(out_tmp), verify=True)

            self.assertEqual(log.status, "success")
            self.assertEqual(log.stop_reason, StopReason.SUCCESS)
            self.assertEqual(repair_calls, [])
            self.assertTrue(log.minimal_passed)
            self.assertFalse(log.strongest_passed)
            self.assertEqual(log.stage_results[1].stage, "strongest_verify")
            self.assertEqual(log.stage_results[1].status, "fail")

    def test_warm_success_and_clean_replay_success_are_recorded_separately(self) -> None:
        plan = BootstrapPlan(
            repo_name="fixture",
            install=[CommandCandidate(kind=CommandKind.INSTALL, command="true", maturity_target=Maturity.INSTALLABILITY)],
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="python -c 'import fixture'", maturity_target=Maturity.INSTALLABILITY),
            strongest_verify=CommandCandidate(kind=CommandKind.STRONGEST_VERIFY, command="python -m pytest", maturity_target=Maturity.TESTABILITY),
        )

        class DummyWarmRunner:
            def __init__(self, config):
                self.config = config

            def close(self) -> None:
                pass

        class WarmVerifier:
            def __init__(self, config):
                self.config = config
                self.runner = None

            def verify(self, repo_dir: Path, log_dir: Path | None = None, round_name: str = "verifier") -> VerifierResult:
                return VerifierResult(
                    status="success",
                    stop_reason=StopReason.SUCCESS,
                    maturity_reached=Maturity.TESTABILITY,
                    minimal_passed=True,
                    strongest_passed=True,
                    traces=[CommandTrace(command="python -m pytest", cwd="/workspace/repo", exit_code=0, elapsed_sec=0.1, stage="strongest_verify")],
                )

        class CleanVerifier:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def verify(self, repo_dir: Path, log_dir: Path | None = None, round_name: str = "verifier") -> VerifierResult:
                self.calls.append(round_name)
                if round_name == "clean_replay":
                    return VerifierResult(
                        status="fail",
                        stop_reason=StopReason.VERIFIER_FAILED,
                        maturity_reached=Maturity.INSTALLABILITY,
                        failed_stage="setup",
                        traces=[CommandTrace(command="bash .bootstrap/setup.sh", cwd="/workspace/repo", exit_code=1, elapsed_sec=0.1, stage="setup")],
                    )
                return VerifierResult(
                    status="success",
                    stop_reason=StopReason.SUCCESS,
                    maturity_reached=Maturity.TESTABILITY,
                    minimal_passed=True,
                    strongest_passed=True,
                    traces=[CommandTrace(command="python -m pytest", cwd="/workspace/repo", exit_code=0, elapsed_sec=0.1, stage="strongest_verify")],
                )

        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as out_tmp:
            repo = Path(repo_tmp)
            (repo / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0.1.0'\n", encoding="utf-8")
            (repo / "fixture.py").write_text("x = 1\n", encoding="utf-8")
            config = RuntimeConfig(warm_repair_container=True)
            orchestrator = BootstrapOrchestrator(config=config, allow_fallback=True)
            clean_verifier = CleanVerifier()
            orchestrator.verifier = clean_verifier  # type: ignore[assignment]
            orchestrator._initial_plan = lambda repo_input, paths, agent_files: plan  # type: ignore[method-assign]
            repair_calls = []

            def clean_repair(current_plan, verifier_result, paths, round):
                repair_calls.append(round)
                return RepairPlan(diagnosis="fix cold setup", plan=current_plan)

            orchestrator._repair_plan = clean_repair  # type: ignore[method-assign]

            with patch("rethink.agents.main.WarmDockerRunner", DummyWarmRunner), patch("rethink.agents.main.Verifier", WarmVerifier):
                log = orchestrator.bootstrap(str(repo), Path(out_tmp), verify=True)

            self.assertEqual(log.status, "success")
            self.assertEqual(log.warm_status, "success")
            self.assertEqual(log.clean_replay_status, "success")
            self.assertEqual(log.clean_replay_repair_count, 1)
            self.assertEqual(log.retry_count, 0)
            self.assertEqual(repair_calls, [1])
            self.assertEqual(clean_verifier.calls, ["clean_replay", "clean_replay_repair_1"])
            self.assertTrue((Path(out_tmp) / "warm_pass" / ".bootstrap" / "setup.sh").exists())
            saved = json.loads((Path(out_tmp) / "warm_pass" / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["warm_status"], "success")

    def test_docker_runner_uses_host_network_by_default(self) -> None:
        runner = DockerRunner(RuntimeConfig())
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".bootstrap").mkdir()
            (repo / ".bootstrap" / "doctor.sh").write_text("true\n", encoding="utf-8")
            completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            with patch.object(runner, "available", return_value=True), patch("subprocess.run", return_value=completed) as run:
                runner.run_script(repo, "doctor.sh", 30)

        command = run.call_args.args[0]
        self.assertIn("--network", command)
        self.assertEqual(command[command.index("--network") + 1], "host")
        for name in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"]:
            self.assertIn(name, command)
            self.assertNotIn(f"{name}=", command)

    def test_docker_runner_allows_network_to_be_disabled(self) -> None:
        runner = DockerRunner(RuntimeConfig(docker_network=None))
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".bootstrap").mkdir()
            (repo / ".bootstrap" / "doctor.sh").write_text("true\n", encoding="utf-8")
            completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            with patch.object(runner, "available", return_value=True), patch("subprocess.run", return_value=completed) as run:
                runner.run_script(repo, "doctor.sh", 30)

        command = run.call_args.args[0]
        self.assertNotIn("--network", command)

    def test_docker_runner_reports_missing_end_marker(self) -> None:
        runner = DockerRunner(RuntimeConfig(docker_network=None))
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as log_tmp:
            repo = Path(repo_tmp)
            (repo / ".bootstrap").mkdir()
            (repo / ".bootstrap" / "setup.sh").write_text("true\n", encoding="utf-8")
            log_path = Path(log_tmp) / "setup.log"
            completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            with patch.object(runner, "available", return_value=True), patch("subprocess.run", return_value=completed):
                traces = runner.run_script_sequence(repo, [("setup.sh", 30, log_path)])

            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0].stage, "setup")
            self.assertEqual(traces[0].exit_code, 125)
            self.assertIn("missing verifier end marker", traces[0].stderr_summary)

    def test_warm_docker_runner_reuses_container_and_cleans_up(self) -> None:
        runner = WarmDockerRunner(RuntimeConfig())
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as log_tmp:
            repo = Path(repo_tmp)
            (repo / ".bootstrap").mkdir()
            log_path = Path(log_tmp) / "warm_setup.log"

            def fake_run(command, **kwargs):
                if command[:2] == ["docker", "exec"]:
                    log_path.write_text("__RETHINK_START__ setup.sh\n__RETHINK_END__ setup.sh exit_code=0 elapsed_sec=1\n", encoding="utf-8")
                return type("Completed", (), {"returncode": 0, "stdout": "container-id", "stderr": ""})()

            with patch.object(runner, "available", return_value=True), patch("subprocess.run", side_effect=fake_run) as run:
                traces = runner.run_script_sequence(repo, [("setup.sh", 30, log_path)])
                runner.close()

            self.assertEqual(len(traces), 1)
            self.assertEqual(traces[0].exit_code, 0)
            calls = [call.args[0] for call in run.call_args_list]
            self.assertEqual(calls[0][:3], ["docker", "run", "-d"])
            self.assertIn("--network", calls[0])
            self.assertEqual(calls[0][calls[0].index("--network") + 1], "host")
            self.assertEqual(calls[1][:3], ["docker", "exec", runner.container_name])
            self.assertEqual(calls[2][:4], ["docker", "rm", "-f", runner.container_name])

    def test_staged_sequence_runs_commands_with_pipefail(self) -> None:
        stages = [
            CommandStage(
                name="strongest_verify",
                command="python -m pytest missing 2>&1 | head -50",
                timeout_sec=30,
                cwd=".",
                maturity_target=Maturity.TESTABILITY,
            )
        ]

        script = _sequence_script(stages, [Path("strongest_verify.log")], "/workspace/repo")

        self.assertIn("set -euo pipefail; git config --global --add safe.directory", script)
        self.assertIn("cd", script)
        self.assertIn("/workspace/repo", script)
        self.assertIn("timeout 30 bash -lc", script)

    def test_staged_trace_detects_failure_output_with_zero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "strongest_verify.log"
            log_path.write_text(
                "__RETHINK_START__ strongest_verify\n"
                "/workspace/repo/.bootstrap/venv/bin/python: No module named pytest\n"
                "__RETHINK_END__ strongest_verify exit_code=0 elapsed_sec=0\n",
                encoding="utf-8",
            )
            stage = CommandStage(
                name="strongest_verify",
                command=".bootstrap/venv/bin/python -m pytest tests 2>&1 | head -50",
                timeout_sec=30,
                maturity_target=Maturity.TESTABILITY,
            )

            traces = _read_sequence_traces([log_path], [stage], "/workspace/repo", 0.1)

        self.assertEqual(traces[0].exit_code, 1)
        self.assertEqual(traces[0].failure_signature.failure_type, "missing_dependency")
        self.assertIn("No module named pytest", traces[0].stdout_summary)

    def test_staged_trace_continues_after_strongest_advisory_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            strongest_log = Path(tmp) / "strongest_verify.log"
            run_probe_log = Path(tmp) / "run_probe.log"
            strongest_log.write_text(
                "__RETHINK_START__ strongest_verify\n"
                "FAILED tests/test_fixture.py::test_value\n"
                "__RETHINK_END__ strongest_verify exit_code=1 elapsed_sec=1\n",
                encoding="utf-8",
            )
            run_probe_log.write_text(
                "__RETHINK_START__ run_probe\n"
                "ok\n"
                "__RETHINK_END__ run_probe exit_code=0 elapsed_sec=1\n",
                encoding="utf-8",
            )
            stages = [
                CommandStage(name="strongest_verify", command="python -m pytest", timeout_sec=30, maturity_target=Maturity.TESTABILITY),
                CommandStage(name="run_probe", command="python -m fixture", timeout_sec=30, maturity_target=Maturity.RUNNABILITY),
            ]

            traces = _read_sequence_traces([strongest_log, run_probe_log], stages, "/workspace/repo", 0.1)

        self.assertEqual([trace.stage for trace in traces], ["strongest_verify", "run_probe"])
        self.assertEqual(traces[0].exit_code, 1)
        self.assertEqual(traces[1].exit_code, 0)

    def test_initial_llm_failure_writes_evaluation_log(self) -> None:
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as out_tmp:
            repo = Path(repo_tmp)
            out = Path(out_tmp)
            (repo / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0.1.0'\n", encoding="utf-8")
            (repo / "fixture.py").write_text("x = 1\n", encoding="utf-8")
            orchestrator = BootstrapOrchestrator(allow_fallback=False)
            orchestrator._llm_client = lambda: (_ for _ in ()).throw(Exception("provider exploded"))  # type: ignore[method-assign]

            log = orchestrator.bootstrap(str(repo), out, verify=False)

            self.assertEqual(log.status, "fail")
            self.assertEqual(log.stop_reason, StopReason.SCHEMA_VALIDATION_FAILED)
            self.assertTrue((out / "evaluation_log.json").exists())
            self.assertIn("provider exploded", (out / "evaluation_log.json").read_text(encoding="utf-8"))
            events = [json.loads(line) for line in (out / "agent_outputs" / "llm_events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["status"], "failed")
            self.assertEqual(events[-1]["error_type"], "Exception")
            self.assertIn("provider exploded", events[-1]["error"])

    def test_run_csv_slice_writes_process_output_to_job_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "repo_run"
            job = _RUN_CSV_SLICE.Job(row_number=1, repo="ignored", name="fixture", language=None, out_dir=out_dir)
            args = argparse.Namespace(
                python=sys.executable,
                model="test:model",
                no_verify=True,
                allow_fallback=False,
                warm_repair=False,
                log_llm_conversations=True,
                allow_clone=True,
            )

            with patch.object(
                _RUN_CSV_SLICE.subprocess,
                "run",
                return_value=type("Completed", (), {"returncode": 0})(),
            ) as run:
                result = _RUN_CSV_SLICE._run_job(job, args)

            self.assertEqual(result.returncode, 0)
            self.assertGreaterEqual(result.duration_sec, 0.0)
            self.assertEqual(result.job.name, "fixture")
            log_path = out_dir / "bootstrap_process.log"
            self.assertTrue(log_path.exists())
            log_text = log_path.read_text(encoding="utf-8")
            self.assertIn("-m rethink.cli bootstrap", log_text)
            self.assertIn("--log-llm-conversations", log_text)
            self.assertEqual(run.call_args.kwargs["stderr"], _RUN_CSV_SLICE.subprocess.STDOUT)

    def test_verifier_writes_script_logs(self) -> None:
        class LoggingRunner:
            scripts_seen: list[str] = []

            def run_script(self, repo_dir: Path, script: str, timeout_sec: int, log_path: Path | None = None) -> CommandTrace:
                self.scripts_seen.append(script)
                if log_path is not None:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path.write_text(f"{script} output\n", encoding="utf-8")
                return CommandTrace(command=f"bash .bootstrap/{script}", cwd="/workspace/repo", exit_code=0, elapsed_sec=0.1)

            def run_script_sequence(self, repo_dir: Path, scripts: list[tuple[str, int, Path | None]]) -> list[CommandTrace]:
                return [self.run_script(repo_dir, script, timeout_sec, log_path) for script, timeout_sec, log_path in scripts]

        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as log_tmp:
            repo = Path(repo_tmp)
            (repo / ".bootstrap").mkdir()
            (repo / ".bootstrap" / "verify.sh").write_text("true\n", encoding="utf-8")
            verifier = Verifier(RuntimeConfig())
            runner = LoggingRunner()
            verifier.runner = runner  # type: ignore[assignment]

            result = verifier.verify(repo, log_dir=Path(log_tmp), round_name="verifier_round_7")

            self.assertEqual(result.status, "success")
            self.assertEqual(runner.scripts_seen, ["setup.sh", "doctor.sh", "verify.sh"])
            self.assertEqual((Path(log_tmp) / "verifier_round_7_doctor.log").read_text(encoding="utf-8"), "doctor.sh output\n")
            self.assertEqual((Path(log_tmp) / "verifier_round_7_setup.log").read_text(encoding="utf-8"), "setup.sh output\n")
            self.assertEqual((Path(log_tmp) / "verifier_round_7_verify.log").read_text(encoding="utf-8"), "verify.sh output\n")

    def test_verifier_runtime_version_check_is_installability_only(self) -> None:
        class PassingRunner:
            def run_script_sequence(self, repo_dir: Path, scripts: list[tuple[str, int, Path | None]]) -> list[CommandTrace]:
                return [
                    CommandTrace(command=f"bash .bootstrap/{script}", cwd="/workspace/repo", exit_code=0, elapsed_sec=0.1)
                    for script, _timeout, _log in scripts
                ]

        with tempfile.TemporaryDirectory() as repo_tmp:
            repo = Path(repo_tmp)
            (repo / ".bootstrap").mkdir()
            (repo / ".bootstrap" / "verify.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\npython3 --version\n", encoding="utf-8")
            verifier = Verifier(RuntimeConfig())
            verifier.runner = PassingRunner()  # type: ignore[assignment]

            result = verifier.verify(repo)

            self.assertEqual(result.status, "success")
            self.assertEqual(result.maturity_reached, Maturity.INSTALLABILITY)

    def test_verifier_stdlib_only_python_check_is_installability_only(self) -> None:
        class PassingRunner:
            def run_script_sequence(self, repo_dir: Path, scripts: list[tuple[str, int, Path | None]]) -> list[CommandTrace]:
                return [
                    CommandTrace(command=f"bash .bootstrap/{script}", cwd="/workspace/repo", exit_code=0, elapsed_sec=0.1)
                    for script, _timeout, _log in scripts
                ]

        with tempfile.TemporaryDirectory() as repo_tmp:
            repo = Path(repo_tmp)
            (repo / ".bootstrap").mkdir()
            (repo / ".bootstrap" / "verify.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n.bootstrap/venv/bin/python -c \"import sys; print(sys.version)\"\n", encoding="utf-8")
            verifier = Verifier(RuntimeConfig())
            verifier.runner = PassingRunner()  # type: ignore[assignment]

            result = verifier.verify(repo)

            self.assertEqual(result.status, "success")
            self.assertEqual(result.maturity_reached, Maturity.INSTALLABILITY)

    def test_verifier_doctor_after_setup_preserves_installability_maturity(self) -> None:
        class DoctorFailingRunner:
            def run_script_sequence(self, repo_dir: Path, scripts: list[tuple[str, int, Path | None]]) -> list[CommandTrace]:
                return [
                    CommandTrace(command="bash .bootstrap/setup.sh", cwd="/workspace/repo", exit_code=0, elapsed_sec=0.1),
                    CommandTrace(command="bash .bootstrap/doctor.sh", cwd="/workspace/repo", exit_code=1, elapsed_sec=0.1),
                ]

        with tempfile.TemporaryDirectory() as repo_tmp:
            repo = Path(repo_tmp)
            (repo / ".bootstrap").mkdir()
            (repo / ".bootstrap" / "verify.sh").write_text("true\n", encoding="utf-8")
            verifier = Verifier(RuntimeConfig())
            verifier.runner = DoctorFailingRunner()  # type: ignore[assignment]

            result = verifier.verify(repo)

            self.assertEqual(result.status, "fail")
            self.assertEqual(result.maturity_reached, Maturity.INSTALLABILITY)

    def test_staged_verifier_treats_strongest_failure_as_advisory(self) -> None:
        class StagedRunner:
            def run_command_sequence(self, repo_dir: Path, stages):
                return [
                    CommandTrace(command=stages[0].command, cwd="/workspace/repo", exit_code=0, elapsed_sec=0.1, stage="setup", maturity_target=Maturity.INSTALLABILITY),
                    CommandTrace(command=stages[1].command, cwd="/workspace/repo", exit_code=0, elapsed_sec=0.1, stage="doctor", maturity_target=Maturity.INSTALLABILITY),
                    CommandTrace(command=stages[2].command, cwd="/workspace/repo", exit_code=0, elapsed_sec=0.1, stage="minimal_verify", maturity_target=Maturity.TESTABILITY),
                    CommandTrace(command=stages[3].command, cwd="/workspace/repo", exit_code=1, elapsed_sec=0.1, stage="strongest_verify", maturity_target=Maturity.TESTABILITY),
                    CommandTrace(command=stages[4].command, cwd="/workspace/repo", exit_code=0, elapsed_sec=0.1, stage="run_probe", maturity_target=Maturity.RUNNABILITY),
                ]

        plan = BootstrapPlan(
            repo_name="fixture",
            install=[CommandCandidate(kind=CommandKind.INSTALL, command="true", maturity_target=Maturity.INSTALLABILITY)],
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="python -c 'import fixture'", maturity_target=Maturity.TESTABILITY),
            strongest_verify=CommandCandidate(kind=CommandKind.STRONGEST_VERIFY, command="python -m pytest", maturity_target=Maturity.TESTABILITY),
            run_probe=CommandCandidate(kind=CommandKind.RUN_PROBE, command="python -m fixture", maturity_target=Maturity.RUNNABILITY),
        )
        with tempfile.TemporaryDirectory() as repo_tmp:
            repo = Path(repo_tmp)
            write_bootstrap(repo, build_manifest(plan))
            verifier = Verifier(RuntimeConfig())
            verifier.runner = StagedRunner()  # type: ignore[assignment]

            result = verifier.verify(repo)

            self.assertEqual(result.status, "success")
            self.assertIsNone(result.failed_stage)
            self.assertTrue(result.minimal_passed)
            self.assertFalse(result.strongest_passed)
            self.assertTrue(result.run_probe_passed)
            self.assertEqual(result.maturity_reached, Maturity.RUNNABILITY)
            self.assertEqual(result.stage_results[3].stage, "strongest_verify")
            self.assertEqual(result.stage_results[3].status, "fail")

    def test_staged_verifier_runs_run_probe_for_runnability(self) -> None:
        class StagedRunner:
            def run_command_sequence(self, repo_dir: Path, stages):
                return [
                    CommandTrace(command=stage.command, cwd="/workspace/repo", exit_code=0, elapsed_sec=0.1, stage=stage.name, maturity_target=stage.maturity_target)
                    for stage in stages
                ]

        plan = BootstrapPlan(
            repo_name="fixture",
            install=[CommandCandidate(kind=CommandKind.INSTALL, command="true", maturity_target=Maturity.INSTALLABILITY)],
            minimal_verify=CommandCandidate(kind=CommandKind.MINIMAL_VERIFY, command="true", maturity_target=Maturity.TESTABILITY),
            run_probe=CommandCandidate(kind=CommandKind.RUN_PROBE, command="fixture --help", maturity_target=Maturity.RUNNABILITY),
        )
        with tempfile.TemporaryDirectory() as repo_tmp:
            repo = Path(repo_tmp)
            write_bootstrap(repo, build_manifest(plan))
            verifier = Verifier(RuntimeConfig())
            verifier.runner = StagedRunner()  # type: ignore[assignment]

            result = verifier.verify(repo)

            self.assertEqual(result.status, "success")
            self.assertTrue(result.minimal_passed)
            self.assertIs(result.run_probe_passed, True)
            self.assertEqual(result.maturity_reached, Maturity.RUNNABILITY)


if __name__ == "__main__":
    unittest.main()
