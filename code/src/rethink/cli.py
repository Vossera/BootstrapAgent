from __future__ import annotations

import argparse
from pathlib import Path

from rethink.agents.main import BootstrapOrchestrator
from rethink.config import RuntimeConfig
from rethink.evaluation.batch import run_batch
from rethink.serialization import write_json
from rethink.verifier.verifier import Verifier


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rethink")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = subparsers.add_parser("bootstrap", help="Generate and optionally verify .bootstrap for one repo.")
    bootstrap_parser.add_argument("--repo", required=True, help="GitHub URL or local repository path.")
    bootstrap_parser.add_argument("--out", required=True, type=Path, help="Run output directory.")
    bootstrap_parser.add_argument("--no-verify", action="store_true", help="Generate .bootstrap without running Docker verifier.")
    bootstrap_parser.add_argument("--language", default=None, help="Optional language hint.")
    bootstrap_parser.add_argument("--model", default=None, help="LangChain model id, for example openai:gpt-4o-mini, deepseek:deepseek-chat, or anthropic:claude-sonnet-4-5.")
    bootstrap_parser.add_argument("--allow-fallback", action="store_true", help="Allow deterministic fallback if the real LLM path is unavailable.")
    bootstrap_parser.add_argument("--warm-repair", action="store_true", help="Reuse one Docker container during repair loops, then run a final clean replay.")
    bootstrap_parser.add_argument("--log-llm-conversations", action="store_true", help="Write full LLM prompts and raw responses under agent_outputs/llm_conversations/.")
    bootstrap_parser.add_argument("--repo-root", type=Path, default=None, help="Local cache root used to resolve GitHub URLs without cloning.")
    bootstrap_parser.add_argument("--no-clone", action="store_true", help="Fail instead of running git clone when --repo is a URL.")

    batch_parser = subparsers.add_parser("batch", help="Run bootstrap generation for projects in a CSV file.")
    batch_parser.add_argument("--csv", required=True, type=Path)
    batch_parser.add_argument("--out", default=Path("runs"), type=Path)
    batch_parser.add_argument("--limit", type=int, default=None)
    batch_parser.add_argument("--no-verify", action="store_true")
    batch_parser.add_argument("--model", default=None)
    batch_parser.add_argument("--allow-fallback", action="store_true")
    batch_parser.add_argument("--log-llm-conversations", action="store_true", help="Write full LLM prompts and raw responses under each run's agent_outputs/llm_conversations/.")
    batch_parser.add_argument("--repo-root", type=Path, default=None, help="Local cache root used to resolve GitHub URLs without cloning.")
    batch_parser.add_argument("--no-clone", action="store_true", help="Fail instead of running git clone for CSV URLs.")

    verify_parser = subparsers.add_parser("verify", help="Run deterministic Docker verifier against an existing .bootstrap.")
    verify_parser.add_argument("--repo", required=True, type=Path)
    verify_parser.add_argument("--out", type=Path, default=None, help="Optional path for verifier result JSON.")

    args = parser.parse_args(argv)
    if args.command == "bootstrap":
        config = RuntimeConfig(llm_model=args.model) if args.model else RuntimeConfig()
        config.warm_repair_container = args.warm_repair
        config.log_llm_conversations = args.log_llm_conversations or config.log_llm_conversations
        log = BootstrapOrchestrator(config=config, allow_fallback=args.allow_fallback).bootstrap(
            args.repo,
            args.out,
            verify=not args.no_verify,
            language_hint=args.language,
            allow_clone=not args.no_clone,
            repo_root=args.repo_root,
        )
        print(log.model_dump_json(indent=2))
        return 0 if log.status == "success" else 1
    if args.command == "batch":
        config = RuntimeConfig(llm_model=args.model) if args.model else RuntimeConfig()
        config.log_llm_conversations = args.log_llm_conversations or config.log_llm_conversations
        logs = run_batch(
            args.csv,
            args.out,
            limit=args.limit,
            verify=not args.no_verify,
            config=config,
            allow_fallback=args.allow_fallback,
            allow_clone=not args.no_clone,
            repo_root=args.repo_root,
        )
        print(f"completed {len(logs)} runs")
        return 0 if all(log.status == "success" for log in logs) else 1
    if args.command == "verify":
        result = Verifier().verify(args.repo)
        if args.out:
            write_json(args.out, result)
        print(result.model_dump_json(indent=2))
        return 0 if result.status == "success" else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
