from __future__ import annotations

import os
import shutil
from pathlib import Path

from pydantic import BaseModel, Field


class Budget(BaseModel):
    max_repair_loops: int = 20
    max_clean_replay_repair_loops: int = 3
    max_strongest_test_repairs: int = 5
    max_llm_structured_retries: int = 5
    max_repair_llm_structured_retries: int = 5
    llm_initial_timeout_sec: int = 300
    llm_repair_timeout_sec: int = 180
    max_shell_commands: int = 80
    max_total_wall_time_sec: int = 3600
    doctor_timeout_sec: int = 120
    setup_timeout_sec: int = 3600
    minimal_verify_timeout_sec: int = 300
    strongest_verify_timeout_sec: int = 1200


class RuntimeConfig(BaseModel):
    docker_image: str = "rethink-bootstrap-base:latest"
    docker_network: str | None = Field(default_factory=lambda: os.getenv("RETHINK_DOCKER_NETWORK", "host") or None)
    workspace_container_path: str = "/workspace/repo"
    budget: Budget = Field(default_factory=Budget)
    llm_model: str = Field(default_factory=lambda: os.getenv("RETHINK_LLM_MODEL", "openai:gpt-4o-mini"))
    warm_repair_container: bool = False
    log_llm_conversations: bool = Field(default_factory=lambda: os.getenv("RETHINK_LOG_LLM_CONVERSATIONS", "").lower() in {"1", "true", "yes", "on"})


def required_api_key_name(model: str) -> str | None:
    provider = model.split(":", 1)[0].lower() if ":" in model else ""
    if provider == "openai":
        return "OPENAI_API_KEY"
    if provider == "anthropic":
        return "ANTHROPIC_API_KEY"
    if provider in {"google", "google_genai", "genai"}:
        return "GOOGLE_API_KEY"
    if provider == "deepseek":
        return "DEEPSEEK_API_KEY"
    return None


def validate_llm_environment(model: str) -> None:
    key_name = required_api_key_name(model)
    if key_name and not os.getenv(key_name):
        raise RuntimeError(f"{key_name} is required for RETHINK_LLM_MODEL={model}")


class RunPaths(BaseModel):
    run_dir: Path
    workspace_dir: Path
    repo_dir: Path
    agent_outputs_dir: Path
    traces_dir: Path

    @classmethod
    def create(cls, run_dir: Path) -> "RunPaths":
        workspace_dir = run_dir / "workspace"
        return cls(
            run_dir=run_dir,
            workspace_dir=workspace_dir,
            repo_dir=workspace_dir / "repo",
            agent_outputs_dir=run_dir / "agent_outputs",
            traces_dir=run_dir / "traces",
        )

    def ensure(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.agent_outputs_dir.mkdir(parents=True, exist_ok=True)
        self.traces_dir.mkdir(parents=True, exist_ok=True)

    def reset_for_run(self) -> None:
        shutil.rmtree(self.agent_outputs_dir, ignore_errors=True)
        shutil.rmtree(self.traces_dir, ignore_errors=True)
        try:
            (self.run_dir / "evaluation_log.json").unlink()
        except FileNotFoundError:
            pass
