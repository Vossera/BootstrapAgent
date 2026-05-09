# Agent-Ready Bootstrap Method

## 方法目标

这个方法接收一个 GitHub 仓库链接或本地 repo，由 agent 自动找出让项目“基本跑起来”的最短可信路径，并把这条路径压缩成一个可复用、可验证的 `.bootstrap`。

它不是另一个写代码 agent，而是一个启动加速层：先替下游 agent 或新手用户完成仓库理解、命令发现、失败诊断和验证路径固化，让后续使用者少花 token、少跑无效命令、少误判项目已经成功启动。

核心输入和产物：

```text
input:
  GitHub link / local repo

output:
  .bootstrap/
  构建.bootstrap过程中的开销
```

其他运行信息不作为方法产物，而是作为 downstream agent 评测时的记录：

```text
evaluation log:
  success or fail
  command trace
  validation maturity
  token cost
  wall-clock time cost
  command count / retry count
```

## Method Pipeline

1. 输入 GitHub link 或本地 repo。
2. Agent 扫描仓库证据，包括 README、CI 配置、lockfile、package metadata、docs、Makefile、脚本目录、项目结构，以及 GitHub Issues 里和安装、启动、测试失败相关的讨论。
3. Agent 生成候选启动路径：依赖安装命令、minimal validation command、strongest local CI-derived validation、可选 runnability probe，以及可能需要的系统依赖或环境变量。
4. Agent 在 Docker 评测镜像里执行候选命令，记录每一步的 cwd、命令、退出码、输出摘要、耗时和失败原因。
5. 执行成功的路径被固化进 `.bootstrap`，失败但有诊断价值的路径进入 failure playbook。
6. `verify` 先运行 minimal command，确认项目至少达到最小可信验证。
7. minimal command 成功后，再运行本地可复现的最强 CI-derived validation，用来确认这条启动路径不是偶然成功。
8. 如果 `verify` 成功，保留 success 和完整 trace 作为 downstream agent 评测记录。
9. 如果 `verify` 失败，把 trace 回传给 agent，agent 根据失败点修复 `.bootstrap`，然后重新执行验证循环。

## Docker Evaluation Environment

方法的执行和评测都基于固定 Docker 镜像，而不是直接使用用户本机环境。镜像应该模拟一个干净但可用的基础开发环境：足够暴露仓库启动难点，但不要把问题退化成安装最基础工具。

推荐镜像预装：

```text
bash / sh
git
curl / wget
ca-certificates
tar / unzip
coreutils
findutils
grep / sed / awk
make
sudo or root-capable package install path
```

语言运行时和项目级依赖默认不预装，除非它们属于该 Docker track 明确规定的基础能力。比如 Python、Node、Go、Rust、Java 等工具链应由 `.bootstrap/setup.sh` 或镜像 track 约定来准备，并在 evidence 中记录。

## `.bootstrap` Contract

`.bootstrap` 是最终产物，应该短、可读、可执行，并且只保存实际验证过的路径。

推荐结构：

```text
.bootstrap/
  setup.sh              # 安装依赖、准备环境、写入必要配置
  doctor.sh             # 在 setup 后快速诊断已准备好的环境
  verify.sh             # 先跑 minimal command，再跑可复现的最强 CI-derived validation
  commands.yaml         # install / minimal / CI-derived / runnability 命令
  evidence_map.yaml     # 每个关键判断来自哪里，以及是否实际执行过
  agent_context.md      # 给下游 agent 的压缩上下文
  failure_playbook.md   # 常见失败、诊断依据和修复路径
```

`.bootstrap` 里的命令需要保留 provenance：它来自 CI、README、lockfile、package metadata、docs，还是来自失败修复。这样下游 agent 不需要重新猜测为什么要跑这个命令。

## Verify Strategy

`verify.sh` 采用两阶段验证：

```text
stage 1:
  run minimal command
  goal: 用最小成本确认项目基本可验证

stage 2:
  run strongest local CI-derived validation
  goal: 在本地可复现范围内尽量接近维护者的真实验证路径
```

minimal command 不一定等于完整 CI。完整 CI 可能依赖 secrets、缓存、服务容器、长时间矩阵任务或外部服务；这里选择的是本地干净环境中可以稳定重放、并且足够有信号的命令。

每个成功命令都标注 maturity：

```text
installability:
  依赖安装、build setup 或 package manager resolution 成功。

testability:
  单元测试、smoke test、lint/type check 或轻量 runtime probe 成功。

runnability:
  主入口、CLI、dev server health check 或端到端主流程可以运行。
```

默认先追求 testability；如果仓库有自包含的主入口或 CLI，再额外追求 runnability。

## Failure Loop

失败不是直接终止，而是进入 trace-driven repair：

```text
verify fail
  -> collect trace
  -> identify failure point
  -> update setup / verify / doctor / commands
  -> rerun verify
```

trace 至少包含：

```text
command
cwd
exit code
stdout/stderr summary
elapsed time
detected environment facts
diagnosis
repair action
retry count
```

Agent 可以修复环境、依赖安装方式、命令顺序、工作目录、版本选择和必要的非源码配置。默认不修改业务源码；如果必须修改配置，需要在 trace 和 evidence 中明确记录原因。

## Trace Memory Architecture

失败后的 trace 不只是日志，而是后续修复的检索入口。系统把失败执行规范化成 failure signature，再从当前运行、历史运行、GitHub Issues 和 CI logs 中检索相似 repair episodes，作为下一轮 `.bootstrap` 修复的候选依据。

角色边界：

```text
Bootstrap Agent:
  retrieval + repair + .bootstrap update

Verifier:
  deterministic execution + trace normalization

Trace Memory:
  indexed repair episodes from current run, past runs, issues, CI logs
```

Verifier 不负责检索，也不负责修复。它只在 Docker 评测镜像里顺序执行 `.bootstrap/setup.sh`、`.bootstrap/doctor.sh` 和 `.bootstrap/verify.sh`，然后输出结构化 trace、pass/fail、maturity 和 stop reason。这样可以保持 verifier 的裁判性质，避免验证阶段临时修复掩盖 `.bootstrap` 本身的质量。

Bootstrap Agent 消费 verifier 输出的 trace，再进行检索和修复：

```text
verify fail
  -> verifier normalizes trace
  -> bootstrap agent retrieves similar failures
  -> bootstrap agent proposes repair actions
  -> bootstrap agent updates .bootstrap
  -> verifier reruns deterministic verification
```

每个 repair episode 至少记录：

```text
failure_signature:
  command
  cwd
  exit_code
  normalized_error_snippet
  language / package_manager
  runtime_version
  failure_type

repair_episode:
  diagnosis
  repair_action
  bootstrap_files_changed
  commands_added_or_removed
  verify_result_after_repair
  maturity_reached
```

检索得到的 repair 只是候选动作，不能直接算成功。只有当更新后的 `.bootstrap/setup.sh` 和 `.bootstrap/verify.sh` 在 Docker 评测镜像里重新通过，修复才被接受。

## Budget and Stop Policy

为了避免 agent 无限探索或无限修复，每次 `.bootstrap` 生成都需要明确 budget。失败时不仅记录 fail，也记录触发了哪一种停止条件。

推荐默认值：

```text
global:
  max_total_wall_time: 60 min
  max_agent_repair_loops: 5
  max_shell_commands: 80
  max_total_retries: 10

discovery:
  max_issue_threads_read: 20
  max_docs_files_read: 30

execution:
  max_candidate_install_commands: 5
  max_candidate_verify_commands: 8
  max_same_command_retries: 1
  max_same_failure_type_retries: 2

timeouts:
  setup_command_timeout: 10 min
  minimal_verify_timeout: 5 min
  strongest_ci_verify_timeout: 20 min
  doctor_timeout: 2 min
```

重试需要区分三类：

```text
same command retry:
  同一个命令最多重试 1 次，只用于网络抖动、下载失败、缓存不稳定等临时问题。

same failure type retry:
  同一类失败最多修复 2 次，避免一直围绕同一个 missing dependency 或版本错误试错。

repair loop:
  verify fail -> collect trace -> repair .bootstrap -> rerun verify
  整体最多 5 轮。
```

停止原因需要写入 evaluation log：

```text
stop_reason:
  success
  max_repair_loops_reached
  max_total_wall_time_reached
  max_shell_commands_reached
  command_timeout
  repeated_same_failure
  unsafe_command_detected
  external_service_required
```

默认策略是 minimal validation 优先：先用较短 timeout 找到最小可信路径，再把更多时间留给本地可复现的最强 CI-derived validation。

## Evaluation Log

`.bootstrap` 是方法产物；下面这些是为了评测 downstream agent 是否真正受益而保留的运行记录。

```text
status:
  success / fail

bootstrap_path:
  .bootstrap/

validation:
  minimal_command
  strongest_local_ci_command
  maturity_reached

cost:
  token_cost
  wall_clock_time
  command_count
  retry_count
  stop_reason

trace:
  executed_commands
  failed_commands
  repair_steps
  final_verify_output
```

成功意味着 `.bootstrap/setup.sh`、`.bootstrap/doctor.sh` 和 `.bootstrap/verify.sh` 可以在同类 Docker 评测镜像里重放，并且 `verify` 达到记录的 maturity。失败也应该留下可读 trace，让下一轮 agent 能从具体失败点继续，而不是从头重新探索。

## Positioning

这个方法的核心价值是把“陌生仓库怎么启动”从一次性的 agent 探索，变成一个紧凑、可重放、带证据来源的启动包。

最终希望下游 agent 或新手用户只需要：

```text
1. run .bootstrap/setup.sh
2. run .bootstrap/doctor.sh
3. run .bootstrap/verify.sh
4. read .bootstrap/agent_context.md if more context is needed
```

如果 `verify` 通过，就可以相信项目至少达到了记录的 maturity；如果失败，就把 trace 交给 agent 继续修复。

## V1 Implementation Shape

第一版实现为评测原型，而不是产品服务。系统分成两条硬边界：

```text
DeepAgents Bootstrap Layer:
  multi-agent reasoning, evidence extraction, command planning, repair planning

Deterministic Verifier:
  Docker execution, trace normalization, pass/fail judgment
```

Bootstrap layer 可以使用 `deepagents` 的 planning、filesystem 和 subagent 能力，但所有 agent 间通信和落盘产物都必须是结构化 JSON/YAML。自然语言只允许出现在 `.bootstrap/agent_context.md` 和 `.bootstrap/failure_playbook.md`。

第一版 agent 分工：

```text
MainBootstrapAgent:
  orchestrates the workflow and repair loop

DiscoveryAgent:
  scans README, docs, package metadata, lockfiles, Makefile, scripts, project layout

CIEvidenceAgent:
  extracts local validation candidates from CI configuration

CommandPlannerAgent:
  emits install, doctor, minimal verify, strongest local CI-derived verify commands

BootstrapWriterAgent:
  turns a validated BootstrapPlan into the .bootstrap contract files

RepairAgent:
  consumes verifier traces and proposes structured repairs
```

Verifier 不调用 LLM，不检索历史，不修复 `.bootstrap`。它只在固定 Docker 镜像中执行：

```text
bash .bootstrap/setup.sh
bash .bootstrap/doctor.sh
bash .bootstrap/verify.sh
```

然后输出 `VerifierResult`，包含 command traces、maturity、stop reason 和 failure signatures。

默认基础镜像由 `docker/bootstrap-base/Dockerfile` 定义，并通过下面命令构建：

```text
scripts/build-bootstrap-base-image.sh rethink-bootstrap-base:latest
```

该镜像只预装通用开发基础命令：bash、git、curl/wget、ca-certificates、tar/unzip、coreutils、findutils、grep/sed、make、sudo 等。语言运行时仍由 `.bootstrap/setup.sh` 或后续 track-specific 镜像负责。

V1 代码接口：

```text
rethink bootstrap --repo <url-or-local-path> --out runs/<project>
rethink batch --csv launcher_projects.csv --limit 3
rethink verify --repo runs/<project>/workspace/repo
```

运行目录结构：

```text
runs/<project>/
  workspace/repo/
  agent_outputs/
    discovery_report.json
    ci_evidence.yaml
    bootstrap_plan.json
    repair_plan_round_<n>.json
  traces/
    verifier_round_<n>.json
  evaluation_log.json
```

当前原型默认要求使用真实 LLM。`deepagents` 和对应 LangChain model provider 必须可用，并且需要配置模型对应的 API key：

```text
RETHINK_LLM_MODEL=openai:gpt-4o-mini
OPENAI_API_KEY=...

# or
RETHINK_LLM_MODEL=anthropic:claude-sonnet-4-5
ANTHROPIC_API_KEY=...

# or
RETHINK_LLM_MODEL=google:gemini-...
GOOGLE_API_KEY=...

# or
RETHINK_LLM_MODEL=deepseek:deepseek-chat
DEEPSEEK_API_KEY=...
```

没有 API key 时，系统返回 `llm_unavailable`，不会静默退回确定性规则。只有显式传入 `--allow-fallback` 时，才允许使用 deterministic fallback subagents，以便离线测试 schema、writer、Docker verifier 和 evaluation loop。
