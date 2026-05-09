from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, Field

from rethink.config import validate_llm_environment
from rethink.agents.prompts import bootstrap_command_constraints, repair_command_constraints
from rethink.schemas import BootstrapPlan, CIEvidenceReport, CommandCandidate, DiscoveryReport, EvidenceItem, RepairPlan, VerifierResult

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    pass


_BOOTSTRAP_COMMAND_CONSTRAINTS = bootstrap_command_constraints()


class RepairListCommandReplacement(BaseModel):
    section: Literal["doctor", "install"]
    index: int = Field(ge=0)
    command: CommandCandidate


class RepairListCommandInsertion(BaseModel):
    section: Literal["doctor", "install"]
    command: CommandCandidate
    index: int | None = Field(default=None, ge=0)


class RepairListCommandRemoval(BaseModel):
    section: Literal["doctor", "install"]
    index: int = Field(ge=0)


class RepairListCommandMove(BaseModel):
    section: Literal["doctor", "install"]
    from_index: int = Field(ge=0)
    to_index: int = Field(ge=0)


class RepairPlanDelta(BaseModel):
    diagnosis: str
    replace_doctor: list[CommandCandidate] | None = None
    replace_install: list[CommandCandidate] | None = None
    replace_minimal_verify: CommandCandidate | None = None
    replace_strongest_verify: CommandCandidate | None = None
    replace_run_probe: CommandCandidate | None = None
    clear_strongest_verify: bool = False
    clear_run_probe: bool = False
    replace_commands: list[RepairListCommandReplacement] = Field(default_factory=list)
    insert_commands: list[RepairListCommandInsertion] = Field(default_factory=list)
    remove_commands: list[RepairListCommandRemoval] = Field(default_factory=list)
    move_commands: list[RepairListCommandMove] = Field(default_factory=list)
    agent_context_append: str = ""
    failure_playbook_append: str = ""
    evidence_additions: list[EvidenceItem] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    commands_added_or_removed: list[str] = Field(default_factory=list)


class DeepAgentsStructuredClient:
    def __init__(self, model: str, conversation_log_dir: Path | None = None, request_timeout_sec: int | None = None) -> None:
        self.model = model
        self.request_timeout_sec = request_timeout_sec
        self.last_usage: dict[str, Any] | None = None
        self.conversation_log_dir = conversation_log_dir
        self.conversation_log_context: dict[str, Any] = {}
        try:
            from langchain.agents import create_agent  # type: ignore
        except Exception as exc:
            raise LLMUnavailable("langchain agents are not installed") from exc
        validate_llm_environment(model)
        self._create_deep_agent = create_agent

    def configure_request_timeout(self, timeout_sec: int | None) -> None:
        self.request_timeout_sec = timeout_sec

    def configure_conversation_logging(self, log_dir: Path | None, **context: Any) -> None:
        self.conversation_log_dir = log_dir
        self.conversation_log_context = {key: value for key, value in context.items() if value is not None}

    def generate_bootstrap_plan(self, discovery: DiscoveryReport, ci: CIEvidenceReport) -> BootstrapPlan:
        constraints = bootstrap_command_constraints(discovery)
        prompt = (
            "Generate a BootstrapPlan JSON for this repository. Use only commands that are plausible "
            "from the provided evidence. Preserve provenance in source/reason fields. The plan must "
            "describe bootstrap commands for the checked-out source. Commands that mutate the checkout "
            "or use remote installers are allowed when necessary and will be logged as safety warnings.\n\n"
            f"{constraints}\n\n"
            f"DiscoveryReport:\n{discovery.model_dump_json(indent=2)}\n\n"
            f"CIEvidenceReport:\n{ci.model_dump_json(indent=2)}\n"
        )
        system_prompt = f"You are CommandPlannerAgent. Return only the structured BootstrapPlan.\n\n{constraints}"
        return self._invoke_structured(BootstrapPlan, system_prompt, prompt)

    def repair_plan(self, plan: BootstrapPlan, verifier_result: VerifierResult) -> RepairPlan:
        constraints = repair_command_constraints(plan)
        prompt = (
            "Generate a RepairPlanDelta JSON from this failed verifier result. Return only the smallest "
            "delta needed to repair the current BootstrapPlan; omitted or null fields are copied from the "
            "current plan locally. Prefer replace_commands for one-command edits, move_commands for reordering, "
            "and insert_commands or remove_commands for list changes. Use replace_doctor or replace_install only when the whole "
            "list must change. For doctor/install list edits, use the zero-based `index` values shown in "
            "the compact JSON and never remove an index that is not present. Only change setup, verify, doctor commands, cwd, timeout, agent_context, "
            "evidence, or failure_playbook. Checkout mutations or remote installers are allowed when needed "
            "for the repository bootstrap and will be logged as safety warnings. Do not call tools or "
            "inspect files; use only the supplied JSON. Focus on the failed command and the shortest repair.\n\n"
            f"Current BootstrapPlan compact JSON:\n{json.dumps(_compact_bootstrap_plan(plan), ensure_ascii=False, indent=2)}\n\n"
            f"VerifierResult compact JSON:\n{json.dumps(_compact_verifier_result(verifier_result), ensure_ascii=False, indent=2)}\n"
        )
        system_prompt = f"You are RepairAgent. Return only the structured RepairPlanDelta. Do not call tools.\n\n{constraints}"
        delta = self._invoke_structured(RepairPlanDelta, system_prompt, prompt)
        return _apply_repair_delta(plan, delta)

    def _invoke_structured(self, schema: type[T], system_prompt: str, prompt: str) -> T:
        input_tokens = _estimate_tokens(system_prompt) + _estimate_tokens(prompt)
        self.last_usage = {
            "input_tokens": input_tokens,
            "output_tokens": 0,
            "total_tokens": input_tokens,
            "estimated": True,
        }
        agent = self._create_deep_agent(
            model=self._chat_model(),
            tools=[],
            system_prompt=system_prompt,
            response_format=schema,
            name=schema.__name__,
        )
        payload = {"messages": [{"role": "user", "content": prompt}]}
        result: Any = None
        try:
            result = agent.invoke(payload)
        except Exception as exc:
            self._write_conversation_log(schema, system_prompt, payload, result=result, error=exc)
            raise
        self._write_conversation_log(schema, system_prompt, payload, result=result)
        self.last_usage = _usage_from_result(result, input_tokens=input_tokens)
        parsed = _extract_structured(result)
        if isinstance(parsed, schema):
            self.last_usage = _usage_with_output_estimate(self.last_usage, parsed.model_dump_json())
            return parsed
        if isinstance(parsed, dict):
            model = schema.model_validate(parsed)
            self.last_usage = _usage_with_output_estimate(self.last_usage, model.model_dump_json())
            return model
        if isinstance(parsed, str):
            if not parsed.strip():
                raise ValueError(f"empty structured response for {schema.__name__}")
            model = schema.model_validate_json(parsed)
            self.last_usage = _usage_with_output_estimate(self.last_usage, parsed)
            return model
        raise ValueError(f"deepagents result did not contain {schema.__name__}: {type(parsed).__name__}")

    def _chat_model(self) -> Any:
        timeout = getattr(self, "request_timeout_sec", None)
        if timeout is None or timeout <= 0:
            return self.model
        provider, _, model_name = self.model.partition(":")
        if not model_name:
            return self.model
        provider = provider.lower()
        if provider == "deepseek":
            from langchain_deepseek import ChatDeepSeek  # type: ignore

            return ChatDeepSeek(model=model_name, timeout=timeout)
        if provider == "openai":
            from langchain_openai import ChatOpenAI  # type: ignore

            return ChatOpenAI(model=model_name, timeout=timeout)
        if provider == "anthropic":
            from langchain_anthropic import ChatAnthropic  # type: ignore

            return ChatAnthropic(model_name=model_name, timeout=timeout)
        if provider in {"google", "google_genai", "genai"}:
            from langchain_google_genai import ChatGoogleGenerativeAI  # type: ignore

            return ChatGoogleGenerativeAI(model=model_name, request_timeout=timeout)
        return self.model

    def _write_conversation_log(
        self,
        schema: type[BaseModel],
        system_prompt: str,
        payload: dict[str, Any],
        *,
        result: Any,
        error: BaseException | None = None,
    ) -> None:
        log_dir = getattr(self, "conversation_log_dir", None)
        if log_dir is None:
            return
        context = dict(getattr(self, "conversation_log_context", {}))
        phase = str(context.get("phase") or "llm")
        attempt = context.get("attempt")
        round_value = context.get("round")
        parts = [phase]
        if round_value is not None:
            parts.append(f"round_{round_value}")
        if attempt is not None:
            parts.append(f"attempt_{attempt}")
        parts.append(schema.__name__)
        path = log_dir / ("_".join(_safe_filename_part(part) for part in parts) + ".json")
        record: dict[str, Any] = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model": self.model,
            "request_timeout_sec": getattr(self, "request_timeout_sec", None),
            "schema": schema.__name__,
            "context": context,
            "system_prompt": system_prompt,
            "request": _jsonable(payload),
            "result": _jsonable(result),
        }
        if error is not None:
            record["error"] = {"type": type(error).__name__, "message": str(error)}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _extract_structured(result: Any) -> Any:
    if isinstance(result, BaseModel):
        return result
    if isinstance(result, dict):
        for key in ("structured_response", "response", "output"):
            if key in result and result[key] is not None:
                return result[key]
        messages = result.get("messages") or []
        if messages:
            content = getattr(messages[-1], "content", None)
            if isinstance(content, str):
                return _maybe_json(content)
            return content
    return result


def _usage_from_result(result: Any, *, input_tokens: int) -> dict[str, Any]:
    actual = _find_usage(result)
    if actual:
        prompt_tokens = _first_int(actual, "prompt_tokens", "input_tokens", "prompt_token_count")
        completion_tokens = _first_int(actual, "completion_tokens", "output_tokens", "completion_token_count")
        total_tokens = _first_int(actual, "total_tokens", "total_token_count")
        if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
            total_tokens = prompt_tokens + completion_tokens
        return {
            "input_tokens": prompt_tokens if prompt_tokens is not None else input_tokens,
            "output_tokens": completion_tokens if completion_tokens is not None else 0,
            "total_tokens": total_tokens if total_tokens is not None else input_tokens,
            "estimated": False,
        }
    return {
        "input_tokens": input_tokens,
        "output_tokens": 0,
        "total_tokens": input_tokens,
        "estimated": True,
    }


def _usage_with_output_estimate(usage: dict[str, Any] | None, output: str) -> dict[str, Any]:
    base = usage or {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "estimated": True}
    if not base.get("estimated") and int(base.get("output_tokens") or 0) > 0:
        return base
    output_tokens = _estimate_tokens(output)
    input_tokens = int(base.get("input_tokens") or 0)
    return {
        **base,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "estimated": bool(base.get("estimated", True)),
    }


def _find_usage(value: Any) -> Any:
    if value is None:
        return None
    for attr in ("usage_metadata", "response_metadata", "usage", "token_usage", "llm_output"):
        if isinstance(value, dict):
            candidate = value.get(attr)
        else:
            candidate = getattr(value, attr, None)
        if candidate:
            if attr == "response_metadata" and isinstance(candidate, dict):
                nested = candidate.get("token_usage") or candidate.get("usage")
                if nested:
                    return nested
            if attr == "llm_output" and isinstance(candidate, dict):
                nested = candidate.get("token_usage") or candidate.get("usage")
                if nested:
                    return nested
            return candidate
    if isinstance(value, dict):
        messages = value.get("messages") or []
        for message in reversed(messages):
            usage = _find_usage(message)
            if usage:
                return usage
    return None


def _first_int(mapping: Any, *keys: str) -> int | None:
    for key in keys:
        if isinstance(mapping, dict):
            value = mapping.get(key)
        else:
            value = getattr(mapping, key, None)
        if isinstance(value, dict):
            nested = _first_int(value, *keys)
            if nested is not None:
                return nested
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _maybe_json(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    content = getattr(value, "content", None)
    if content is not None:
        data: dict[str, Any] = {"type": type(value).__name__, "content": _jsonable(content)}
        for attr in ("role", "name", "id", "usage_metadata", "response_metadata"):
            attr_value = getattr(value, attr, None)
            if attr_value is not None:
                data[attr] = _jsonable(attr_value)
        return data
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:
            pass
    return repr(value)


def _safe_filename_part(value: object) -> str:
    text = str(value)
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in text).strip("_") or "value"


def _apply_repair_delta(plan: BootstrapPlan, delta: RepairPlanDelta) -> RepairPlan:
    repaired = plan.model_copy(deep=True)
    if delta.replace_doctor is not None:
        repaired.doctor = list(delta.replace_doctor)
    if delta.replace_install is not None:
        repaired.install = list(delta.replace_install)
    if delta.replace_minimal_verify is not None:
        repaired.minimal_verify = delta.replace_minimal_verify
    if delta.clear_strongest_verify:
        repaired.strongest_verify = None
    elif delta.replace_strongest_verify is not None:
        repaired.strongest_verify = delta.replace_strongest_verify
    if delta.clear_run_probe:
        repaired.run_probe = None
    elif delta.replace_run_probe is not None:
        repaired.run_probe = delta.replace_run_probe

    for removal in sorted(delta.remove_commands, key=lambda item: item.index, reverse=True):
        commands = _repair_command_list(repaired, removal.section)
        if removal.index >= len(commands):
            raise ValueError(f"repair delta removal index out of range: {removal.section}[{removal.index}]")
        del commands[removal.index]
    for replacement in delta.replace_commands:
        commands = _repair_command_list(repaired, replacement.section)
        if replacement.index >= len(commands):
            raise ValueError(f"repair delta replacement index out of range: {replacement.section}[{replacement.index}]")
        commands[replacement.index] = replacement.command
    for move in delta.move_commands:
        commands = _repair_command_list(repaired, move.section)
        if move.from_index >= len(commands):
            raise ValueError(f"repair delta move source index out of range: {move.section}[{move.from_index}]")
        command = commands.pop(move.from_index)
        if move.to_index > len(commands):
            raise ValueError(f"repair delta move target index out of range: {move.section}[{move.to_index}]")
        commands.insert(move.to_index, command)
    for insertion in delta.insert_commands:
        commands = _repair_command_list(repaired, insertion.section)
        index = len(commands) if insertion.index is None else insertion.index
        if index > len(commands):
            raise ValueError(f"repair delta insertion index out of range: {insertion.section}[{index}]")
        commands.insert(index, insertion.command)

    if delta.agent_context_append.strip():
        repaired.agent_context = _append_paragraph(repaired.agent_context, delta.agent_context_append)
    if delta.failure_playbook_append.strip():
        repaired.failure_playbook = _append_paragraph(repaired.failure_playbook, delta.failure_playbook_append)
    if delta.evidence_additions:
        repaired.evidence.extend(delta.evidence_additions)

    return RepairPlan(
        diagnosis=delta.diagnosis,
        plan=repaired,
        changed_files=delta.changed_files,
        commands_added_or_removed=delta.commands_added_or_removed,
    )


def _repair_command_list(plan: BootstrapPlan, section: Literal["doctor", "install"]) -> list[CommandCandidate]:
    return plan.doctor if section == "doctor" else plan.install


def _append_paragraph(existing: str, addition: str) -> str:
    existing = existing.rstrip()
    addition = addition.strip()
    if not existing:
        return addition
    return f"{existing}\n\n{addition}"


def _compact_bootstrap_plan(plan: BootstrapPlan) -> dict[str, Any]:
    return {
        "repo_name": plan.repo_name,
        "doctor": [_compact_indexed_command(index, command) for index, command in enumerate(plan.doctor)],
        "install": [_compact_indexed_command(index, command) for index, command in enumerate(plan.install)],
        "minimal_verify": _compact_command(plan.minimal_verify),
        "strongest_verify": _compact_command(plan.strongest_verify) if plan.strongest_verify else None,
        "run_probe": _compact_command(plan.run_probe) if plan.run_probe else None,
        "agent_context": _truncate(plan.agent_context, 1200),
        "failure_playbook": _truncate(plan.failure_playbook, 1200),
        "evidence": [
            {
                "source": item.source,
                "path": item.path,
                "summary": _truncate(item.summary, 500),
            }
            for item in plan.evidence[-8:]
        ],
    }


def _compact_indexed_command(index: int, command: Any) -> dict[str, Any]:
    compact = _compact_command(command) or {}
    return {"index": index, **compact}


def _compact_command(command: Any) -> dict[str, Any] | None:
    if command is None:
        return None
    return {
        "kind": command.kind,
        "cwd": command.cwd,
        "command": command.command,
        "source": command.source,
        "confidence": command.confidence,
        "timeout_sec": command.timeout_sec,
        "maturity_target": command.maturity_target,
        "reason": _truncate(command.reason, 500),
    }


def _compact_verifier_result(result: VerifierResult) -> dict[str, Any]:
    failed_trace = next((trace for trace in result.traces if trace.exit_code not in {0, None} or trace.timeout), None)
    if failed_trace is None and result.traces:
        failed_trace = result.traces[-1]
    repair_hints = _repair_hints(result, failed_trace)
    return {
        "status": result.status,
        "stop_reason": result.stop_reason,
        "maturity_reached": result.maturity_reached,
        "trace_summary": [
            {
                "command": trace.command,
                "exit_code": trace.exit_code,
                "timeout": trace.timeout,
                "elapsed_sec": trace.elapsed_sec,
            }
            for trace in result.traces
        ],
        "failed_trace": _compact_trace(failed_trace) if failed_trace else None,
        "repair_hints": repair_hints,
    }


def _compact_trace(trace: Any) -> dict[str, Any]:
    failure_signature = trace.failure_signature.model_dump(mode="json") if trace.failure_signature else None
    if failure_signature and "normalized_error_snippet" in failure_signature:
        failure_signature["normalized_error_snippet"] = _truncate(failure_signature["normalized_error_snippet"], 2500)
    return {
        "command": trace.command,
        "cwd": trace.cwd,
        "exit_code": trace.exit_code,
        "elapsed_sec": trace.elapsed_sec,
        "timeout": trace.timeout,
        "stdout_summary": _truncate(trace.stdout_summary, 5000),
        "stderr_summary": _truncate(trace.stderr_summary, 2500),
        "failure_signature": failure_signature,
    }


def _repair_hints(result: VerifierResult, failed_trace: Any | None) -> list[str]:
    if failed_trace is None:
        return []
    text = f"{failed_trace.stdout_summary}\n{failed_trace.stderr_summary}"
    hints: list[str] = []
    if _looks_like_source_tree_shadowing(failed_trace.command, text):
        hints.extend(
            [
                "possible source-tree shadowing: verify ran from /workspace/repo and traceback imports from /workspace/repo/<package>/..., so Python may be importing the source tree instead of the installed site-packages package",
                "for non-editable Python installs, consider running import verification from outside the repo, e.g. `cd /tmp && /workspace/repo/.bootstrap/venv/bin/python -c \"import package\"`",
                "do not run pytest against `/workspace/repo/<package>` after a non-editable install; use an editable/in-place development install or verify the installed package from outside the repo",
            ]
        )
    return hints


def _looks_like_source_tree_shadowing(command: str, text: str) -> bool:
    if "setup.sh" in command:
        return False
    if "/workspace/repo/" not in text:
        return False
    import_failure = any(
        marker in text
        for marker in [
            "ModuleNotFoundError",
            "ImportError",
            "cannot import name",
        ]
    )
    if not import_failure:
        return False
    return "site-packages" not in text or text.find("/workspace/repo/") < text.find("site-packages")


def _truncate(value: str | None, limit: int) -> str:
    if not value:
        return ""
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...[truncated {len(value) - limit} chars]"
