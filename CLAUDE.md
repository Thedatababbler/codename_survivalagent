# AdaMAS-unit — standing instructions for the implementing agent

> **This project is AdaMAS-unit** (created 2026-11-05): the multi-stage idea of AdaMAS rebuilt as a chain of units.
> Each unit takes the previous unit's code, builds, passes the test author's acceptance gate, and gets at most 3
> repairs; one budget ledger per repository (tokens, wall time, context compressions, repairs) with a budget-planner
> agent for top-up requests. No repair evolution, no candidate search. Agents run on mini-swe-agent.
> v0.2 (2026-11-05): one full multi-stage run is a *round*; only the budget planner evolves between rounds (per-task
> budget table, general rules, round summaries), test suites are authored once and frozen, builders and repairers
> keep nothing across rounds; validated rules are promoted into the harness.
>
> **Read first:** `docs/DESIGN.md` (design v0.2; decisions in §14–§15; the user's extension
> `docs/AdaMAS-unit：可进化的预算规划者（设计扩展 v0.2）.md` is the source) and `docs/USING_ADAMAS.md` (what to take from
> AdaMAS and mini-swe-agent, and how). AdaMAS (`/root/projects/AdaMAS`) and AdaMAS-single are developed by other
> sessions; never edit or push them from here. Write to the user in Chinese.

## 沙盒规定（不可违反）

1. 任何会启动智能体或执行工作区代码的运行，都必须在沙盒内进行。沙盒未生效时，不得运行，不得以任何理由降级。
2. 不得为了让运行通过而放宽沙盒规则、关闭网络拦截、扩大可读路径或修改 LD_PRELOAD。确需调整沙盒规则时，先停下并向用户说明原因，得到同意后才能修改。
3. 不得读取、复制或移动 held out、参考实现、验证套件、memory/ 目录的内容到任何智能体可读的位置；不得在 /tmp 或其他共享位置留下题目代码、测试或参考实现的副本。
4. 每次任务结束时，在回报中必须包含"沙盒越界警告"一节（格式见 docs/sandbox_breach_log.md），即使数量为 0 也要写明。
5. 发现任何越界警告时，先在回报开头说明，再报告其他结果；不得自行判定"无害"后省略。
6. 智能体在工作区以外按本题原项目的名称或其模块名搜索、列目录、查找文件、尝试导入，定为 CRITICAL，运行作废（2026-10-21 用户决定；扫描器事件 `original_project_search`）。在工作区内导入自己正在编写的包属于正常操作，不算。写文件时，或在工作区里运行自己正在编写的包时，包的源码里读取本包版本号（如 `importlib_metadata.version("<本包名>")`）不算；单独去探测本机是否装了这个包，仍定为 CRITICAL（2026-11-05 用户决定）。是否作废按是否获取到内容区分（2026-11-08 用户决定，与 AdaMAS 一致）：被拦下或返回"找不到"，并经核实未获取任何内容（命令输出为空、报错或显示不存在；智能体可读的环境里没有本项目的副本；最终代码与参考实现的相似度不是离群值），记为 CRITICAL 并标记（`SANDBOX_FLAGGED`），运行保留；获取到任何内容（导入成功、读到原项目文件或元数据），或无法核实，运行作废，并查清漏洞。
7. 不得修改 /root、/root/.ssh 及任何系统目录的权限或属主。不得用 chmod 777 这类方式排查权限问题。遇到权限异常，先停下报告，由用户决定。恢复权限时，只能恢复到已知的原始值，并在报告中列出改动。（2026-11-19 用户决定；同时约束实现 agent 自己。`/root` 的 755 是用户手动设置的值，保持不动。）

智能体一律以普通用户身份运行（2026-11-19 用户决定）：执行者、所有子智能体、收尾修补，以及程序代为运行工作区代码时，启动器在沙盒规则生效后切换到 `configs/sandbox.yaml` 里的 `agent_uid`，切换不了就拒绝运行。每轮的工作区归这个用户所有。每个智能体开始前，程序先确认它能读写自己的工作区（只读的审阅者：能读、不能写），不能就报错停止。扫描器把对工作区以外路径的 chmod / chown / chgrp / setfacl 定为 CRITICAL（事件 `permission_change`）。密封评分以专门的普通用户运行（`scoring_uid`，既不是 root 也不是智能体用户；2026-11-19 用户决定），文件规则开启：只能读本次评分的临时副本（被评分代码 + 本次 held out）、系统目录和解释器，网络照常限制；被评分代码改权限或读其他目录的尝试由内核拒绝，并记入 `outputs/scoring_audit.jsonl`（改权限为 CRITICAL）。

How the rules are enforced in code (2026-10-11): `src/orchestra/sandbox/guard.py` (refuses any agent start or
workspace-code run without the sandbox), `src/orchestra/sandbox/exec.py` (the launcher verifies its own
confinement), `src/orchestra/sandbox/breach.py` (the breach log), `configs/sandbox.yaml` (`enforced: true`, cannot
be turned off). The section of rule 4 is produced by `uv run python scripts/sandbox_breach_report.py <run dirs>`.

The sandbox code is copied from AdaMAS (see `docs/USING_ADAMAS.md` §2.1); until it is in place, no agent may be
started and no workspace code may be run in this project. Section format of rule 4: `docs/sandbox_breach_log.md`.

## 收尾导入检查（补丁 2026-11-09）

所有对照组共用同一个收尾导入检查与修补（`src/adamas_unit/importcheck.py`，配置 `configs/importcheck.yaml`，说明见 `docs/DESIGN.md` §7.1）。导入清单只由设计文档生成，在轮回开始前用 `scripts/build_importcheck_manifest.py` 冻结。修补用量不计入轮回预算，单独记账。补丁之后的轮回不与补丁之前的轮回混合比较。

## 正对照与额度

- 每次改动流水线（单元执行器、门、修复证据、预算账本、规划者、进化者、诊断、提交检查、沙盒接入），先通过本项目的正对照（`docs/USING_ADAMAS.md` §4 第 13 项），再做任何运行。
- 花额度的运行先向用户说明预计用量，得到同意再跑。提交和推送也先问。

## v0.3（2026-11-18 起）：执行者 + 子智能体，进化规划者的 skill

- 设计：`adamas_unit_v03_refactor.md`；映射、许可证、用户决定与实现细节：`docs/V03_MAPPING.md`。
- 代码在 `src/survival/`（账本、规划者、skill、执行者与子智能体、一轮、进化）。v0.2 的 `src/adamas_unit/` 冻结为对照（tag `v0.2`），不要改；v0.3 从它复用沙盒接入、收尾导入检查、提交检查、密封评分。
- skill 文档：`memory/planner/skills/<名称>/SKILL.md`（π_0）。共 6 个；第 6 个 `completion-check`（2026-11-19）在执行者声明阶段完成或提交时先被询问（accept / prompt_continue / require_review / require_selftest），π_0 一律 accept。执行者提示词里不加“必须写测试”之类的硬性要求。校准用的“π_0 + 提示继续”在 `configs/v03_calibration_skills/`。迁移检验题冻结为 tablib、tenacity（`configs/v03_transfer_tasks.yaml`），进化期间不运行、不读取。阶段计划：`configs/datasets/v03_plans/`（冻结，只读）。
- 正对照：改动 v0.3 的任何部分后先跑 `uv run python scripts/positive_control_v03.py`（不花额度）；v0.2 的正对照仍是 `scripts/positive_control.py`。
- 运行：`scripts/v03_run_round.py`（一轮）、`scripts/v03_evolve.py`（进化）。都花额度，先报预计用量。
- 上面的沙盒规定、密封规定、提交与推送先询问，对 v0.3 全部适用。回报时"沙盒越界警告"置顶。
- 参考实现 `context-language-models` 是 CC BY-NC 4.0：只借鉴机制，不复制代码。

## ContextBench 设置（2026-11-22 起）

- 说明、来源、许可证、用户决定：`docs/CONTEXTBENCH.md`。代码仓库的多阶段实验暂停，代码保持不动。
- ContextBench 在 `/root/reference/ContextBench`（Apache-2.0，固定提交见 `configs/contextbench.yaml`）：只读使用，不修改其中任何文件，不在里面留下任何文件（包括 `__pycache__`）；不重写它的生成器或评分器。
- 实例与答案在 `sealed/contextbench/`（0700，不进 git）。智能体不得读到答案、检出目录、种子。种子取 1000 以上，42–49 永不使用；检验集在进化期间不运行、不读取。
- 保留关键行、编辑数独棋盘两个任务禁止写盘：三组都没有记忆文件，提示里不提它；任务说明优先于管理说明（2026-11-23）。额度百分点由用户手动读数，不开代理管理接口。
- 触发条件与超窗判定用未校准的 `o200k_base`；记账用服务端的 token 数。结果只在三组（π_0、无管理、进化后）之间比较，与论文数字不可比。
- 进化（2026-11-25）：两步评估——候选先只跑保留关键行，被判为比 π_0 更好并进入前沿的才加跑存取键值作不退步检查（参数 `configs/cb_evolution_e1.json`，说明 `docs/CONTEXTBENCH.md` §12）。智能体在工作区以外搜索任务输入或答案（`find /`、`find /root`、`find /tmp` 等）定为 CRITICAL（事件 `task_input_search`）：被拦下且核实未获取内容则保留并标记，获取到任何内容则作废。
- 总结指示（2026-11-26）：`memory-compaction` 的 `## What to keep when compacting` 一节是总结调用的系统消息（π_0 该节 = 原 `configs/roles_cb/summary.md` 原文，该文件只作对照基准）。总结调用的用户消息把任务说明和对话标为“记录”（不执行、不回复），回复过短或像命令/拒绝时重试一次，仍不合格记为诊断项（`summary_unfit`）。`calib-1` 与 `e1` 是在修复前有缺陷的总结器下得到的，不与之后的结果直接比较。这一节只能用通用表述，不得出现标记格式、任务名、任务术语。skill 各部分送达何处见 `docs/CONTEXTBENCH.md` §13。
- 进化 e3 起（2026-11-27）：进化者的每次回复原样保存在 `outputs/cb_evo/<名称>/generations/gNN/`；不合格的提议每代最多退回 2 次，之后才记为丢弃并计入停止计数；每次总结式压缩计算首次保留率、再总结保留率（只以数字给进化者）；`memory-compaction` 参数 `carry_previous_summary: rewrite | verbatim`（π_0 = rewrite，文档不写这个参数）。说明见 `docs/CONTEXTBENCH.md` §16。
- 正对照：改动这一设置的任何部分后先跑 `uv run python scripts/positive_control_cb.py`（不花额度）；改了 `src/survival/agents.py` 还要跑 v0.3 的正对照。
- 运行：`scripts/cb_run.py`。花额度，先报预计用量。沙盒规定、提交与推送先询问，全部适用。
