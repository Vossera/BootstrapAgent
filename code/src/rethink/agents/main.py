from __future__ import annotations

import json
import re
import shutil
import signal
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any, Callable, TypeVar

from pydantic import ValidationError

from rethink.agents.llm import DeepAgentsStructuredClient, LLMUnavailable
from rethink.agents.subagents import CIEvidenceAgent, CommandPlannerAgent, DiscoveryAgent, RepairAgent
from rethink.agents.tools import build_bootstrap_manifest, write_bootstrap_files
from rethink.bootstrap.writer import BootstrapWriteError
from rethink.config import RunPaths, RuntimeConfig
from rethink.repo import parse_repo_input, prepare_workspace
from rethink.schemas import (
    BootstrapPlan,
    CommandCandidate,
    CommandTrace,
    DiscoveryReport,
    EvaluationLog,
    Maturity,
    RepoInput,
    RepairPlan,
    StopReason,
    VerifierResult,
)
from rethink.serialization import write_json, write_yaml
from rethink.verifier.docker_runner import WarmDockerRunner
from rethink.verifier.verifier import Verifier

T = TypeVar("T")


class PlanSanityError(ValueError):
    pass


class BootstrapOrchestrator:
    def __init__(self, config: RuntimeConfig | None = None, allow_fallback: bool = False) -> None:
        self.config = config or RuntimeConfig()
        self.allow_fallback = allow_fallback
        self.fallback_used = False
        self.llm: DeepAgentsStructuredClient | None = None
        self.discovery_agent = DiscoveryAgent()
        self.ci_agent = CIEvidenceAgent()
        self.planner_agent = CommandPlannerAgent()
        self.repair_agent = RepairAgent()
        self.verifier = Verifier(self.config)
        self._llm_usage_total: dict[str, Any] = _empty_llm_usage()
        self._llm_conversation_log_dir: Path | None = None
        self._llm_context: dict[str, Any] = {}
        self._llm_request_timeout_sec: int | None = None

    def bootstrap(
        self,
        source: str,
        out_dir: Path,
        verify: bool = True,
        language_hint: str | None = None,
        *,
        allow_clone: bool = True,
        repo_root: Path | None = None,
    ) -> EvaluationLog:
        started = time.monotonic()
        self._llm_usage_total = _empty_llm_usage()
        self._progress(f"starting bootstrap source={source} out={out_dir}")
        repo = parse_repo_input(source, language_hint=language_hint)
        paths = RunPaths.create(out_dir)
        paths.reset_for_run()
        paths.ensure()
        self._progress(f"preparing workspace for {repo.name}")
        prepare_workspace(repo, paths.repo_dir, allow_clone=allow_clone, repo_root=repo_root)

        agent_files: list[str] = []
        trace_files: list[str] = []
        retry_count = 0
        command_count = 0
        strongest_test_repair_count = 0
        strongest_floor = 0
        forced_stop_reason: StopReason | None = None
        forced_metadata: dict[str, Any] = {}
        warm_result: VerifierResult | None = None
        clean_replay_result: VerifierResult | None = None
        clean_replay_repair_count = 0

        try:
            plan = self._initial_plan(repo, paths, agent_files)
            self._progress("writing .bootstrap files")
            write_bootstrap_files(paths.repo_dir, build_bootstrap_manifest(plan))
        except Exception as exc:
            self._progress(f"failed before verification: {type(exc).__name__}: {exc}")
            if isinstance(exc, BootstrapWriteError):
                reason = exc.reason
            elif isinstance(exc, LLMUnavailable) or "API_KEY" in str(exc):
                reason = StopReason.LLM_UNAVAILABLE
            else:
                reason = StopReason.SCHEMA_VALIDATION_FAILED
            return self._evaluation_log(
                plan=None,
                paths=paths,
                started=started,
                status="fail",
                stop_reason=reason,
                trace_files=trace_files,
                agent_files=agent_files,
                retry_count=retry_count,
                metadata={"error": str(exc), "repo": repo.model_dump(mode="json")},
            )

        if not verify:
            self._progress("verification skipped")
            return self._evaluation_log(
                plan=plan,
                paths=paths,
                started=started,
                status="success",
                stop_reason=StopReason.SUCCESS,
                trace_files=trace_files,
                agent_files=agent_files,
                retry_count=retry_count,
                metadata={
                    "verification_skipped": True,
                    "repo": repo.model_dump(mode="json"),
                    "llm_model": self.config.llm_model,
                    "fallback_used": self.fallback_used,
                },
            )

        loop_verifier = self.verifier
        warm_runner: WarmDockerRunner | None = None
        if self.config.warm_repair_container:
            self._progress("starting warm repair container for verifier rounds")
            warm_runner = WarmDockerRunner(self.config)
            loop_verifier = Verifier(self.config)
            loop_verifier.runner = warm_runner  # type: ignore[assignment]

        try:
            self._progress("running verifier round 0")
            verifier_result = loop_verifier.verify(paths.repo_dir, log_dir=paths.traces_dir, round_name="verifier_round_0")
            command_count += len(verifier_result.traces)
            self._progress(f"verifier round 0 finished status={verifier_result.status} stop_reason={verifier_result.stop_reason.value}")
            if verifier_result.status == "fail":
                self._progress(_verifier_failure_summary(verifier_result, paths.traces_dir, "verifier_round_0"))
            trace_path = paths.traces_dir / "verifier_round_0.json"
            write_json(trace_path, verifier_result)
            trace_files.append(str(trace_path))

            strongest_floor = _strongest_strength(plan.strongest_verify)
            while _can_repair(verifier_result) and retry_count < self.config.budget.max_repair_loops:
                if _is_strongest_test_failure(verifier_result):
                    if strongest_test_repair_count >= self.config.budget.max_strongest_test_repairs:
                        forced_stop_reason = StopReason.STRONGEST_TEST_REPAIR_LIMIT_REACHED
                        forced_metadata = {
                            "strongest_test_repair_limit_reached": True,
                            "strongest_test_repair_count": strongest_test_repair_count,
                            "strongest_residual_failure": _strongest_residual_failure(verifier_result),
                        }
                        self._progress(
                            "strongest test repair limit reached; stopping with residual strongest failure "
                            f"count={strongest_test_repair_count}"
                        )
                        break
                    strongest_test_repair_count += 1
                retry_count += 1
                self._progress(f"repair round {retry_count} starting from stop_reason={verifier_result.stop_reason.value}")
                try:
                    repair = self._repair_plan(plan, verifier_result, paths, retry_count)
                    repair = _repair_with_validated_strongest(repair, plan, strongest_floor, self._progress)
                except Exception as exc:
                    self._progress(f"repair round {retry_count} failed: {type(exc).__name__}: {exc}")
                    reason = _repair_exception_stop_reason(exc, verifier_result)
                    metadata: dict[str, Any] = {
                        "error": str(exc),
                        "repo": repo.model_dump(mode="json"),
                        "llm_model": self.config.llm_model,
                        **_bootstrap_safety_metadata(paths.repo_dir),
                        **_verifier_metadata(verifier_result),
                    }
                    if isinstance(exc, PlanSanityError):
                        metadata["repair_plan_rejected_by_sanity_check"] = True
                        metadata["underlying_verifier_stop_reason"] = verifier_result.stop_reason.value
                    return self._evaluation_log(
                        plan=plan,
                        paths=paths,
                        started=started,
                        status="fail",
                        stop_reason=reason,
                        trace_files=trace_files,
                        agent_files=agent_files,
                        retry_count=retry_count,
                        maturity=verifier_result.maturity_reached,
                        command_count=command_count,
                        metadata=metadata,
                    )
                plan = repair.plan
                strongest_floor = max(strongest_floor, _strongest_strength(plan.strongest_verify))
                repair_path = paths.agent_outputs_dir / f"repair_plan_round_{retry_count}.json"
                write_json(repair_path, repair)
                agent_files.append(str(repair_path))
                try:
                    self._progress(f"writing repaired .bootstrap files for round {retry_count}")
                    write_bootstrap_files(paths.repo_dir, build_bootstrap_manifest(plan))
                except BootstrapWriteError as exc:
                    self._progress(f"repaired .bootstrap rejected: {exc}")
                    return self._evaluation_log(
                        plan=plan,
                        paths=paths,
                        started=started,
                        status="fail",
                        stop_reason=exc.reason,
                        trace_files=trace_files,
                        agent_files=agent_files,
                        retry_count=retry_count,
                        maturity=verifier_result.maturity_reached,
                        command_count=command_count,
                        metadata={
                            "error": str(exc),
                            "repo": repo.model_dump(mode="json"),
                            **_bootstrap_safety_metadata(paths.repo_dir),
                            **_verifier_metadata(verifier_result),
                        },
                    )
                self._progress(f"running verifier round {retry_count}")
                verifier_result = loop_verifier.verify(paths.repo_dir, log_dir=paths.traces_dir, round_name=f"verifier_round_{retry_count}")
                command_count += len(verifier_result.traces)
                self._progress(f"verifier round {retry_count} finished status={verifier_result.status} stop_reason={verifier_result.stop_reason.value}")
                if verifier_result.status == "fail":
                    self._progress(_verifier_failure_summary(verifier_result, paths.traces_dir, f"verifier_round_{retry_count}"))
                trace_path = paths.traces_dir / f"verifier_round_{retry_count}.json"
                write_json(trace_path, verifier_result)
                trace_files.append(str(trace_path))
        finally:
            if warm_runner is not None:
                self._progress("stopping warm repair container")
                warm_runner.close()

        if self.config.warm_repair_container and verifier_result.status == "success":
            warm_result = verifier_result
            warm_pass_round = retry_count
            warm_pass_plan = plan.model_copy(deep=True)
            warm_pass_dir = paths.run_dir / "warm_pass"
            self._progress(f"warm verifier passed at round {warm_pass_round}; freezing warm .bootstrap files")
            _freeze_warm_pass(paths.repo_dir, warm_pass_dir, warm_pass_plan, warm_result, warm_pass_round)
            agent_files.append(str(warm_pass_dir / "bootstrap_plan.json"))

            self._progress("warm verifier passed; running final clean replay")
            write_bootstrap_files(paths.repo_dir, build_bootstrap_manifest(warm_pass_plan))
            verifier_result = self.verifier.verify(paths.repo_dir, log_dir=paths.traces_dir, round_name="clean_replay")
            clean_replay_result = verifier_result
            command_count += len(verifier_result.traces)
            self._progress(f"clean replay finished status={verifier_result.status} stop_reason={verifier_result.stop_reason.value}")
            if verifier_result.status == "fail":
                self._progress(_verifier_failure_summary(verifier_result, paths.traces_dir, "clean_replay"))
            trace_path = paths.traces_dir / "clean_replay.json"
            write_json(trace_path, verifier_result)
            trace_files.append(str(trace_path))
            plan = warm_pass_plan
            while _can_repair(verifier_result) and clean_replay_repair_count < self.config.budget.max_clean_replay_repair_loops:
                if _is_strongest_test_failure(verifier_result):
                    if strongest_test_repair_count >= self.config.budget.max_strongest_test_repairs:
                        forced_stop_reason = StopReason.STRONGEST_TEST_REPAIR_LIMIT_REACHED
                        forced_metadata = {
                            "strongest_test_repair_limit_reached": True,
                            "strongest_test_repair_count": strongest_test_repair_count,
                            "strongest_residual_failure": _strongest_residual_failure(verifier_result),
                        }
                        self._progress(
                            "strongest test repair limit reached during clean replay repair; stopping with residual "
                            f"strongest failure count={strongest_test_repair_count}"
                        )
                        break
                    strongest_test_repair_count += 1
                clean_replay_repair_count += 1
                self._progress(
                    f"clean replay repair round {clean_replay_repair_count} starting from "
                    f"stop_reason={verifier_result.stop_reason.value}"
                )
                try:
                    repair = self._repair_plan(plan, verifier_result, paths, clean_replay_repair_count)
                    repair = _repair_with_validated_strongest(repair, plan, strongest_floor, self._progress)
                except Exception as exc:
                    self._progress(f"clean replay repair round {clean_replay_repair_count} failed: {type(exc).__name__}: {exc}")
                    reason = _repair_exception_stop_reason(exc, verifier_result)
                    metadata: dict[str, Any] = {
                        "error": str(exc),
                        "repo": repo.model_dump(mode="json"),
                        "llm_model": self.config.llm_model,
                        **_bootstrap_safety_metadata(paths.repo_dir),
                        **_warm_clean_metadata(warm_result, clean_replay_result, clean_replay_repair_count),
                        **_verifier_metadata(verifier_result),
                    }
                    if isinstance(exc, PlanSanityError):
                        metadata["repair_plan_rejected_by_sanity_check"] = True
                        metadata["underlying_verifier_stop_reason"] = verifier_result.stop_reason.value
                    return self._evaluation_log(
                        plan=plan,
                        paths=paths,
                        started=started,
                        status="fail",
                        stop_reason=reason,
                        trace_files=trace_files,
                        agent_files=agent_files,
                        retry_count=retry_count,
                        maturity=verifier_result.maturity_reached,
                        command_count=command_count,
                        warm_result=warm_result,
                        clean_replay_result=clean_replay_result,
                        clean_replay_repair_count=clean_replay_repair_count,
                        metadata=metadata,
                    )
                plan = repair.plan
                strongest_floor = max(strongest_floor, _strongest_strength(plan.strongest_verify))
                repair_path = paths.agent_outputs_dir / f"clean_replay_repair_plan_round_{clean_replay_repair_count}.json"
                write_json(repair_path, repair)
                agent_files.append(str(repair_path))
                try:
                    self._progress(f"writing clean-replay repaired .bootstrap files for round {clean_replay_repair_count}")
                    write_bootstrap_files(paths.repo_dir, build_bootstrap_manifest(plan))
                except BootstrapWriteError as exc:
                    self._progress(f"clean-replay repaired .bootstrap rejected: {exc}")
                    return self._evaluation_log(
                        plan=plan,
                        paths=paths,
                        started=started,
                        status="fail",
                        stop_reason=exc.reason,
                        trace_files=trace_files,
                        agent_files=agent_files,
                        retry_count=retry_count,
                        maturity=verifier_result.maturity_reached,
                        command_count=command_count,
                        warm_result=warm_result,
                        clean_replay_result=clean_replay_result,
                        clean_replay_repair_count=clean_replay_repair_count,
                        metadata={
                            "error": str(exc),
                            "repo": repo.model_dump(mode="json"),
                            **_bootstrap_safety_metadata(paths.repo_dir),
                            **_warm_clean_metadata(warm_result, clean_replay_result, clean_replay_repair_count),
                            **_verifier_metadata(verifier_result),
                        },
                    )
                round_name = f"clean_replay_repair_{clean_replay_repair_count}"
                self._progress(f"running {round_name}")
                verifier_result = self.verifier.verify(paths.repo_dir, log_dir=paths.traces_dir, round_name=round_name)
                clean_replay_result = verifier_result
                command_count += len(verifier_result.traces)
                self._progress(f"{round_name} finished status={verifier_result.status} stop_reason={verifier_result.stop_reason.value}")
                if verifier_result.status == "fail":
                    self._progress(_verifier_failure_summary(verifier_result, paths.traces_dir, round_name))
                trace_path = paths.traces_dir / f"{round_name}.json"
                write_json(trace_path, verifier_result)
                trace_files.append(str(trace_path))
                if verifier_result.status == "success":
                    break

        status = "success" if verifier_result.status == "success" else "fail"
        stop_reason = forced_stop_reason or verifier_result.stop_reason
        if forced_stop_reason is None and status == "fail" and _can_repair(verifier_result) and retry_count >= self.config.budget.max_repair_loops:
            stop_reason = StopReason.MAX_REPAIR_LOOPS_REACHED
        if (
            forced_stop_reason is None
            and self.config.warm_repair_container
            and warm_result is not None
            and status == "fail"
            and _can_repair(verifier_result)
            and clean_replay_repair_count >= self.config.budget.max_clean_replay_repair_loops
        ):
            stop_reason = StopReason.CLEAN_REPLAY_REPAIR_LIMIT_REACHED

        self._progress(f"finished status={status} stop_reason={stop_reason.value}")
        return self._evaluation_log(
            plan=plan,
            paths=paths,
            started=started,
            status=status,
            stop_reason=stop_reason,
            trace_files=trace_files,
            agent_files=agent_files,
            retry_count=retry_count,
            maturity=verifier_result.maturity_reached,
            command_count=command_count,
            warm_result=warm_result,
            clean_replay_result=clean_replay_result,
            clean_replay_repair_count=clean_replay_repair_count,
            metadata={
                "repo": repo.model_dump(mode="json"),
                "llm_model": self.config.llm_model,
                "fallback_used": self.fallback_used,
                "warm_repair_container": self.config.warm_repair_container,
                **_bootstrap_safety_metadata(paths.repo_dir),
                **_warm_clean_metadata(warm_result, clean_replay_result, clean_replay_repair_count),
                "strongest_test_repair_count": strongest_test_repair_count,
                "strongest_strength_floor": strongest_floor,
                **forced_metadata,
                **_verifier_metadata(verifier_result),
            },
        )

    def _initial_plan(self, repo: RepoInput, paths: RunPaths, agent_files: list[str]) -> BootstrapPlan:
        self._progress("running discovery")
        discovery = self.discovery_agent.run(paths.repo_dir, progress=self._progress)
        discovery = discovery.model_copy(update={"repo_name": repo.name})
        if repo.language_hint:
            discovery = _apply_language_hint(discovery, repo.language_hint)
        discovery_path = paths.agent_outputs_dir / "discovery_report.json"
        write_json(discovery_path, discovery)
        agent_files.append(str(discovery_path))

        self._progress("collecting CI evidence")
        ci = self.ci_agent.run(paths.repo_dir)
        ci_path = paths.agent_outputs_dir / "ci_evidence.yaml"
        write_yaml(ci_path, ci)
        agent_files.append(str(ci_path))

        try:
            self._progress(f"requesting initial plan from LLM model={self.config.llm_model}")
            plan = self._invoke_llm_with_retries(
                paths,
                phase="initial_plan",
                call=lambda: self._llm_client().generate_bootstrap_plan(discovery, ci),
            )
            _validate_plan_against_discovery(plan, discovery)
        except Exception as exc:
            if not self.allow_fallback:
                raise
            self.fallback_used = True
            self._progress(f"LLM initial plan unavailable; using fallback planner: {type(exc).__name__}: {exc}")
            plan = self.planner_agent.run(discovery, ci)
            _write_llm_marker(paths, "initial_plan", "fallback", model=self.config.llm_model, error=exc)
        plan_path = paths.agent_outputs_dir / "bootstrap_plan.json"
        write_json(plan_path, plan)
        agent_files.append(str(plan_path))
        return plan

    def _repair_plan(self, plan: BootstrapPlan, verifier_result: VerifierResult, paths: RunPaths, round: int):
        try:
            self._progress(f"requesting repair plan round {round} from LLM model={self.config.llm_model}")
            repair = self._invoke_llm_with_retries(
                paths,
                phase="repair_plan",
                round=round,
                call=lambda: self._llm_client().repair_plan(plan, verifier_result),
            )
            discovery = _load_discovery_report(paths)
            if discovery is not None:
                _validate_plan_against_discovery(repair.plan, discovery)
            return repair
        except Exception as exc:
            if not self.allow_fallback:
                raise
            self.fallback_used = True
            self._progress(f"LLM repair round {round} unavailable; using fallback repair: {type(exc).__name__}: {exc}")
            repair = self.repair_agent.run(plan, verifier_result)
            _write_llm_marker(paths, "repair_plan", "fallback", round=round, model=self.config.llm_model, error=exc)
            return repair

    def _llm_client(self) -> DeepAgentsStructuredClient:
        if self.llm is None:
            self.llm = DeepAgentsStructuredClient(self.config.llm_model, request_timeout_sec=self._llm_request_timeout_sec)
        elif hasattr(self.llm, "configure_request_timeout"):
            self.llm.configure_request_timeout(self._llm_request_timeout_sec)
        if self.config.log_llm_conversations and hasattr(self.llm, "configure_conversation_logging"):
            self.llm.configure_conversation_logging(self._llm_conversation_log_dir, **self._llm_context)
        elif hasattr(self.llm, "configure_conversation_logging"):
            self.llm.configure_conversation_logging(None)
        return self.llm

    def _invoke_llm_with_retries(
        self,
        paths: RunPaths,
        phase: str,
        call: Callable[[], T],
        round: int | None = None,
    ) -> T:
        retries = self.config.budget.max_repair_llm_structured_retries if phase == "repair_plan" else self.config.budget.max_llm_structured_retries
        attempts = retries + 1
        timeout_sec = self.config.budget.llm_repair_timeout_sec if phase == "repair_plan" else self.config.budget.llm_initial_timeout_sec
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            llm_started = _write_llm_marker(paths, phase, "running", round=round, model=self.config.llm_model, attempt=attempt)
            self._configure_llm_conversation_logging(paths, phase=phase, round=round, attempt=attempt)
            self._progress(f"LLM {phase} attempt {attempt}/{attempts} started timeout={timeout_sec}s" + (f" round={round}" if round is not None else ""))
            self._llm_request_timeout_sec = timeout_sec
            try:
                with _llm_timeout(timeout_sec, phase):
                    result = call()
                usage = self._consume_llm_usage()
                _write_llm_marker(paths, phase, "success", round=round, model=self.config.llm_model, started_at=llm_started, attempt=attempt, usage=usage)
                self._progress(f"LLM {phase} attempt {attempt}/{attempts} succeeded")
                return result
            except Exception as exc:
                last_error = exc
                usage = self._consume_llm_usage()
                if not _should_retry_llm_error(exc) or attempt >= attempts:
                    _write_llm_marker(paths, phase, "failed", round=round, model=self.config.llm_model, started_at=llm_started, error=exc, attempt=attempt, usage=usage)
                    self._progress(f"LLM {phase} attempt {attempt}/{attempts} failed: {type(exc).__name__}: {exc}")
                    raise
                _write_llm_marker(paths, phase, "retrying", round=round, model=self.config.llm_model, started_at=llm_started, error=exc, attempt=attempt, usage=usage)
                self._progress(f"LLM {phase} attempt {attempt}/{attempts} failed; retrying: {type(exc).__name__}: {exc}")
        raise RuntimeError("LLM invocation failed without an exception") from last_error

    def _configure_llm_conversation_logging(self, paths: RunPaths, *, phase: str, round: int | None, attempt: int) -> None:
        if not self.config.log_llm_conversations:
            self._llm_conversation_log_dir = None
            self._llm_context = {}
            if self.llm is not None and hasattr(self.llm, "configure_conversation_logging"):
                self.llm.configure_conversation_logging(None)
            return
        self._llm_conversation_log_dir = paths.agent_outputs_dir / "llm_conversations"
        self._llm_context = {"phase": phase, "round": round, "attempt": attempt}
        if self.llm is not None and hasattr(self.llm, "configure_conversation_logging"):
            self.llm.configure_conversation_logging(self._llm_conversation_log_dir, **self._llm_context)

    def _consume_llm_usage(self) -> dict[str, Any] | None:
        client = self.llm
        usage = getattr(client, "last_usage", None)
        if not isinstance(usage, dict):
            return None
        getattr(client, "__dict__", {})["last_usage"] = None
        normalized = _normalize_llm_usage(usage)
        _add_llm_usage(self._llm_usage_total, normalized)
        return normalized

    def _progress(self, message: str) -> None:
        print(f"[rethink] {message}", file=sys.stderr, flush=True)

    def _evaluation_log(
        self,
        plan: BootstrapPlan | None,
        paths: RunPaths,
        started: float,
        status: str,
        stop_reason: StopReason,
        trace_files: list[str],
        agent_files: list[str],
        retry_count: int,
        maturity: Any = None,
        command_count: int = 0,
        warm_result: VerifierResult | None = None,
        clean_replay_result: VerifierResult | None = None,
        clean_replay_repair_count: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> EvaluationLog:
        log = EvaluationLog(
            status="success" if status == "success" else "fail",
            bootstrap_path=str(paths.repo_dir / ".bootstrap"),
            warm_status=warm_result.status if warm_result else None,
            warm_stop_reason=warm_result.stop_reason if warm_result else None,
            clean_replay_status=clean_replay_result.status if clean_replay_result else None,
            clean_replay_stop_reason=clean_replay_result.stop_reason if clean_replay_result else None,
            clean_replay_repair_count=clean_replay_repair_count,
            minimal_command=plan.minimal_verify.command if plan else None,
            strongest_local_ci_command=plan.strongest_verify.command if plan and plan.strongest_verify else None,
            run_probe_command=plan.run_probe.command if plan and plan.run_probe else None,
            maturity_reached=maturity or "none",
            failed_stage=metadata.get("failed_stage") if metadata else None,
            minimal_passed=bool(metadata.get("minimal_passed")) if metadata and "minimal_passed" in metadata else False,
            strongest_passed=bool(metadata.get("strongest_passed")) if metadata and "strongest_passed" in metadata else False,
            run_probe_passed=metadata.get("run_probe_passed") if metadata else None,
            stage_results=metadata.get("stage_results", []) if metadata else [],
            token_cost=float(self._llm_usage_total["total_tokens"]) if self._llm_usage_total["total_tokens"] else None,
            wall_clock_time_sec=time.monotonic() - started,
            command_count=command_count,
            retry_count=retry_count,
            stop_reason=stop_reason,
            trace_files=trace_files,
            agent_output_files=agent_files,
            metadata={**(metadata or {}), "llm_usage_total": dict(self._llm_usage_total)},
        )
        write_json(paths.run_dir / "evaluation_log.json", log)
        return log


def _can_repair(verifier_result: VerifierResult) -> bool:
    if verifier_result.status != "fail":
        return False
    return verifier_result.stop_reason in {
        StopReason.VERIFIER_FAILED,
        StopReason.COMMAND_TIMEOUT,
    }


def _freeze_warm_pass(repo_dir: Path, warm_pass_dir: Path, plan: BootstrapPlan, verifier_result: VerifierResult, round: int) -> None:
    if warm_pass_dir.exists():
        shutil.rmtree(warm_pass_dir)
    warm_pass_dir.mkdir(parents=True, exist_ok=True)
    bootstrap_src = repo_dir / ".bootstrap"
    bootstrap_dst = warm_pass_dir / ".bootstrap"
    if bootstrap_src.exists():
        shutil.copytree(bootstrap_src, bootstrap_dst)
    write_json(warm_pass_dir / "bootstrap_plan.json", plan)
    write_json(
        warm_pass_dir / "metadata.json",
        {
            "warm_pass_round": round,
            "warm_status": verifier_result.status,
            "warm_stop_reason": verifier_result.stop_reason.value,
            "warm_maturity_reached": verifier_result.maturity_reached.value,
            **_verifier_metadata(verifier_result),
        },
    )


def _warm_clean_metadata(
    warm_result: VerifierResult | None,
    clean_replay_result: VerifierResult | None,
    clean_replay_repair_count: int,
) -> dict[str, Any]:
    return {
        "warm_status": warm_result.status if warm_result else None,
        "warm_stop_reason": warm_result.stop_reason.value if warm_result else None,
        "warm_maturity_reached": warm_result.maturity_reached.value if warm_result else None,
        "clean_replay_status": clean_replay_result.status if clean_replay_result else None,
        "clean_replay_stop_reason": clean_replay_result.stop_reason.value if clean_replay_result else None,
        "clean_replay_maturity_reached": clean_replay_result.maturity_reached.value if clean_replay_result else None,
        "clean_replay_repair_count": clean_replay_repair_count,
    }


def _bootstrap_safety_metadata(repo_dir: Path) -> dict[str, Any]:
    path = repo_dir / ".bootstrap" / "safety_warnings.json"
    try:
        warnings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        warnings = []
    if not isinstance(warnings, list):
        warnings = []
    return {
        "safety_warning_count": len(warnings),
        "safety_warnings": warnings,
    }


def _is_strongest_test_failure(verifier_result: VerifierResult) -> bool:
    if verifier_result.failed_stage != "strongest_verify":
        return False
    trace = _failed_trace_for_stage(verifier_result, "strongest_verify")
    if trace is None:
        return False
    if trace.failure_signature and trace.failure_signature.failure_type == "test_failure":
        return True
    return _looks_like_test_failure_text(f"{trace.stdout_summary}\n{trace.stderr_summary}")


def _validate_strongest_not_downgraded(plan: BootstrapPlan, floor: int) -> None:
    strength = _strongest_strength(plan.strongest_verify)
    if floor > 0 and strength < floor:
        command = plan.strongest_verify.command if plan.strongest_verify else "<removed>"
        raise PlanSanityError(f"repair plan downgrades strongest_verify below prior strength: {command}")
    if plan.strongest_verify and _strongest_swallows_test_failures(plan.strongest_verify.command):
        raise PlanSanityError(f"strongest_verify masks test failures: {plan.strongest_verify.command}")


def _repair_with_validated_strongest(
    repair: RepairPlan,
    previous_plan: BootstrapPlan,
    strongest_floor: int,
    progress: Callable[[str], None],
) -> RepairPlan:
    try:
        _validate_strongest_not_downgraded(repair.plan, strongest_floor)
        return repair
    except PlanSanityError as exc:
        fixed_plan = repair.plan.model_copy(deep=True)
        fixed_plan.strongest_verify = (
            previous_plan.strongest_verify.model_copy(deep=True)
            if previous_plan.strongest_verify is not None
            else None
        )
        _validate_strongest_not_downgraded(fixed_plan, strongest_floor)
        progress(f"repair attempted invalid strongest_verify change; preserving previous strongest_verify: {exc}")
        return repair.model_copy(
            update={
                "diagnosis": (
                    f"{repair.diagnosis}\n\n"
                    f"Preserved previous strongest_verify because the repair attempted an invalid strongest change: {exc}"
                ),
                "plan": fixed_plan,
            }
        )


def _strongest_strength(command: CommandCandidate | None) -> int:
    if command is None:
        return 0
    text = f"{command.command}\n{command.reason}".lower()
    if _is_test_command_text(text):
        return 50
    if _is_runnable_command_text(text) or command.maturity_target == Maturity.RUNNABILITY:
        return 40
    if _is_compile_command_text(text):
        return 30
    if _is_import_command_text(text):
        return 20
    if _is_static_check_command_text(text) or command.maturity_target == Maturity.INSTALLABILITY:
        return 10
    return 15


def _is_test_command_text(text: str) -> bool:
    return any(
        re.search(pattern, text)
        for pattern in [
            r"\bpytest\b",
            r"\bunittest\b",
            r"\bmvn(?:w|)\s+[^;&|]*\btest\b",
            r"\bgradle(?:w|)\s+[^;&|]*\btest\b",
            r"\bnpm\s+(?:run\s+)?test\b",
            r"\byarn\s+(?:run\s+)?test\b",
            r"\bpnpm\s+(?:run\s+)?test\b",
            r"\bgo\s+test\b",
            r"\bcargo\s+test\b",
            r"\bdotnet\s+test\b",
            r"\bctest\b",
            r"\bmake\s+[^;&|]*test\b",
            r"\bbazel\s+test\b",
            r"\bsbt\s+test\b",
            r"\bmix\s+test\b",
        ]
    )


def _strongest_swallows_test_failures(command: str) -> bool:
    lowered = " ".join(command.lower().split())
    if not _is_test_command_text(lowered):
        return False
    forbidden_tokens = [
        "maven.test.failure.ignore=true",
        "test.failure.ignore=true",
        "-dmaven.test.failure.ignore",
        "-dtest.failure.ignore",
        "--continue",
        "--passwithnotests",
        "--allow-empty",
        "|| true",
        "|| :",
        "; true",
        "&& true",
        "exit 0",
    ]
    return any(token in lowered for token in forbidden_tokens)


def _is_compile_command_text(text: str) -> bool:
    return any(
        re.search(pattern, text)
        for pattern in [
            r"\bmvn(?:w|)\s+[^;&|]*\bcompile\b",
            r"\bgradle(?:w|)\s+[^;&|]*\b(?:assemble|build|compile)\b",
            r"\bnpm\s+run\s+build\b",
            r"\byarn\s+(?:run\s+)?build\b",
            r"\bpnpm\s+(?:run\s+)?build\b",
            r"\bgo\s+build\b",
            r"\bcargo\s+(?:build|check)\b",
            r"\bbazel\s+build\b",
            r"\bmake\b",
            r"\bcmake\b",
        ]
    )


def _is_runnable_command_text(text: str) -> bool:
    return any(token in text for token in ["curl ", "listen", "server", "run_probe", "healthcheck"])


def _is_import_command_text(text: str) -> bool:
    return " -c " in text and ("import " in text or "require(" in text)


def _is_static_check_command_text(text: str) -> bool:
    stripped = " ".join(text.split())
    return any(stripped.startswith(prefix) for prefix in ["test -f ", "test -x ", "ls ", "find ", "grep ", "head ", "cat "])


def _looks_like_test_failure_text(text: str) -> bool:
    lowered = text.lower()
    if re.search(r"(?m)^failed\s+\S+", lowered):
        return True
    return any(
        marker in lowered
        for marker in [
            "there are test failures",
            "<<< failure!",
            "[error] failures:",
            "failed tests:",
            "short test summary info",
            "test failed",
            "tests failed",
            "failure! -- in ",
            "failures!!!",
        ]
    )


def _strongest_residual_failure(verifier_result: VerifierResult) -> dict[str, Any]:
    trace = _failed_trace_for_stage(verifier_result, "strongest_verify")
    if trace is None:
        return {}
    text = f"{trace.stderr_summary}\n{trace.stdout_summary}"
    failed_tests = _extract_failed_tests(text)
    return {
        "stage": "strongest_verify",
        "command": trace.command,
        "exit_code": trace.exit_code,
        "failure_type": trace.failure_signature.failure_type if trace.failure_signature else None,
        "failed_tests": failed_tests,
        "reason": _extract_failure_reason(text, failed_tests),
    }


def _failed_trace_for_stage(verifier_result: VerifierResult, stage: str) -> CommandTrace | None:
    for trace in verifier_result.traces:
        if trace.stage == stage and (trace.exit_code not in {0, None} or trace.timeout or trace.failure_signature is not None):
            return trace
    return None


def _extract_failed_tests(text: str) -> list[dict[str, str]]:
    tests: list[dict[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        maven = re.search(r"^\[ERROR\]\s+([A-Za-z0-9_.$]+)\.([A-Za-z0-9_$]+)(?::\d+)?\s*(.*)$", stripped)
        if maven and "tests run:" not in stripped.lower():
            tests.append({"class": maven.group(1), "method": maven.group(2), "reason": maven.group(3).strip()})
            continue
        pytest = re.search(r"^FAILED\s+([^\s]+?)(?:::([^\s]+))?(?:\s+-\s+(.+))?$", stripped)
        if pytest:
            tests.append({"test": pytest.group(1), "method": pytest.group(2) or "", "reason": pytest.group(3) or ""})
            continue
        suite = re.search(r"<<< FAILURE! -- in\s+([A-Za-z0-9_.$]+)", stripped)
        if suite:
            tests.append({"class": suite.group(1), "method": "", "reason": "test suite failure"})
    return tests[:20]


def _extract_failure_reason(text: str, failed_tests: list[dict[str, str]]) -> str:
    for test in failed_tests:
        reason = test.get("reason", "").strip()
        if reason and reason != "test suite failure":
            return reason[:500]
    interesting = []
    for line in text.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if any(token in lowered for token in ["error", "failure", "failed", "expected", "assert"]):
            interesting.append(stripped)
    return "\n".join(interesting[-5:])[:800]


def _verifier_metadata(verifier_result: VerifierResult) -> dict[str, Any]:
    return {
        "failed_stage": verifier_result.failed_stage,
        "minimal_passed": verifier_result.minimal_passed,
        "strongest_passed": verifier_result.strongest_passed,
        "run_probe_passed": verifier_result.run_probe_passed,
        "stage_results": [stage.model_dump(mode="json") for stage in verifier_result.stage_results],
    }


def _verifier_failure_summary(verifier_result: VerifierResult, trace_dir: Path, round_name: str) -> str:
    failed = None
    if verifier_result.failed_stage:
        failed = next((trace for trace in verifier_result.traces if trace.stage == verifier_result.failed_stage), None)
    if failed is None:
        failed = next((trace for trace in verifier_result.traces if trace.exit_code not in {0, None} or trace.timeout), None)
    if failed is None:
        failed = next((trace for trace in verifier_result.traces if trace.failure_signature is not None), None)
    stage = verifier_result.failed_stage or (failed.stage if failed else "unknown")
    exit_code = failed.exit_code if failed else None
    failure_type = failed.failure_signature.failure_type if failed and failed.failure_signature else "unknown"
    log_path = trace_dir / f"{round_name}_{stage}.log" if stage else trace_dir
    return f"verifier failure detail stage={stage} exit_code={exit_code} failure_type={failure_type} log={log_path}"


def _repair_exception_stop_reason(exc: Exception, verifier_result: VerifierResult) -> StopReason:
    if isinstance(exc, LLMUnavailable) or "API_KEY" in str(exc):
        return StopReason.LLM_UNAVAILABLE
    if isinstance(exc, PlanSanityError):
        return verifier_result.stop_reason
    if isinstance(exc, ValidationError):
        return StopReason.SCHEMA_VALIDATION_FAILED
    return StopReason.SCHEMA_VALIDATION_FAILED


def _apply_language_hint(discovery: DiscoveryReport, language_hint: str) -> DiscoveryReport:
    normalized = _normalize_language(language_hint)
    if not normalized or normalized in discovery.languages:
        return discovery
    return discovery.model_copy(update={"languages": [normalized, *discovery.languages]})


def _normalize_language(language: str) -> str | None:
    lowered = language.strip().lower()
    if lowered in {"c++", "cpp", "cc", "c/c++"}:
        return "C/C++"
    if lowered in {"js", "javascript", "typescript", "ts"}:
        return "JavaScript"
    if lowered in {"py", "python"}:
        return "Python"
    if lowered == "java":
        return "Java"
    if lowered == "go":
        return "Go"
    if lowered == "rust":
        return "Rust"
    return language.strip() or None


def _validate_plan_against_discovery(plan: BootstrapPlan, discovery: DiscoveryReport) -> None:
    if not discovery.important_files:
        return

    context = f"{plan.agent_context}\n{plan.failure_playbook}".lower()
    if "repo is empty" in context or "repository is empty" in context or "no source files" in context:
        raise PlanSanityError("plan contradicts discovery: repository has important files but plan claims it is empty")

    commands = [*plan.doctor, *plan.install, plan.minimal_verify]
    if plan.strongest_verify:
        commands.append(plan.strongest_verify)
    if plan.run_probe:
        commands.append(plan.run_probe)
    if any(_is_empty_repo_command(command) for command in commands):
        raise PlanSanityError("plan contradicts discovery: command claims no source files exist")

    if _is_runtime_version_only(plan.minimal_verify) and (plan.strongest_verify is None or _is_runtime_version_only(plan.strongest_verify)):
        raise PlanSanityError("plan is vacuous: minimal/strongest verification only checks runtime version")


def _load_discovery_report(paths: RunPaths) -> DiscoveryReport | None:
    try:
        return DiscoveryReport.model_validate_json((paths.agent_outputs_dir / "discovery_report.json").read_text(encoding="utf-8"))
    except OSError:
        return None


def _is_empty_repo_command(command: CommandCandidate) -> bool:
    lowered = f"{command.command}\n{command.reason}".lower()
    return "repo is empty" in lowered or "repository is empty" in lowered or "no source files" in lowered


def _is_runtime_version_only(command: CommandCandidate) -> bool:
    normalized = " ".join(command.command.strip().split())
    if normalized in {
        "python --version",
        "python3 --version",
        ".bootstrap/venv/bin/python --version",
        "node --version",
        "npm --version",
        "java -version",
        "gcc --version",
        "g++ --version",
    }:
        return True
    lowered = normalized.lower()
    if "python" in lowered and " -c " in lowered and "import sys" in lowered and not _imports_project_module(normalized):
        return True
    return False


def _imports_project_module(command: str) -> bool:
    lowered = _python_inline_code(command).lower()
    stdlib_only = {"sys", "os", "pathlib", "subprocess", "json", "platform", "importlib", "site"}
    imports = []
    for part in lowered.replace(";", "\n").splitlines():
        stripped = part.strip()
        if stripped.startswith("import "):
            imports.extend(item.strip().split()[0].split(".")[0] for item in stripped.removeprefix("import ").split(","))
        if stripped.startswith("from "):
            imports.append(stripped.removeprefix("from ").split()[0].split(".")[0])
    return any(module and module not in stdlib_only for module in imports)


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


def _should_retry_llm_error(error: Exception) -> bool:
    if isinstance(error, LLMUnavailable):
        return False
    if "API_KEY" in str(error):
        return False
    return True


def _empty_llm_usage() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
        "estimated_calls": 0,
    }


def _normalize_llm_usage(usage: dict[str, Any]) -> dict[str, Any]:
    input_tokens = _usage_int(usage.get("input_tokens"))
    output_tokens = _usage_int(usage.get("output_tokens"))
    total_tokens = _usage_int(usage.get("total_tokens")) or input_tokens + output_tokens
    estimated = bool(usage.get("estimated", True))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated": estimated,
    }


def _usage_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _add_llm_usage(total: dict[str, Any], usage: dict[str, Any]) -> None:
    total["input_tokens"] += usage["input_tokens"]
    total["output_tokens"] += usage["output_tokens"]
    total["total_tokens"] += usage["total_tokens"]
    total["calls"] += 1
    if usage["estimated"]:
        total["estimated_calls"] += 1


@contextmanager
def _llm_timeout(timeout_sec: int, phase: str):
    if timeout_sec <= 0 or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _raise_timeout(signum: int, frame: Any) -> None:
        raise TimeoutError(f"LLM {phase} timed out after {timeout_sec}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _raise_timeout)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_sec)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


def _write_llm_marker(
    paths: RunPaths,
    phase: str,
    status: str,
    round: int | None = None,
    model: str | None = None,
    started_at: str | None = None,
    error: BaseException | None = None,
    attempt: int | None = None,
    usage: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    started = _parse_iso_datetime(started_at) if started_at else now
    payload: dict[str, Any] = {
        "phase": phase,
        "status": status,
        "updated_at": now.isoformat(),
        "started_at": started.isoformat(),
    }
    if round is not None:
        payload["round"] = round
    if attempt is not None:
        payload["attempt"] = attempt
    if model is not None:
        payload["model"] = model
    if status != "running":
        payload["elapsed_sec"] = max(0.0, (now - started).total_seconds())
    if error is not None:
        payload["error_type"] = type(error).__name__
        payload["error"] = str(error)
    if usage is not None:
        payload["llm_usage"] = usage
    write_json(paths.agent_outputs_dir / "llm_status.json", payload)
    _append_jsonl(paths.agent_outputs_dir / "llm_events.jsonl", payload)
    return payload["started_at"]


def _parse_iso_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
