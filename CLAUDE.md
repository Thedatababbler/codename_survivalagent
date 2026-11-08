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

How the rules are enforced in code (2026-10-11): `src/orchestra/sandbox/guard.py` (refuses any agent start or
workspace-code run without the sandbox), `src/orchestra/sandbox/exec.py` (the launcher verifies its own
confinement), `src/orchestra/sandbox/breach.py` (the breach log), `configs/sandbox.yaml` (`enforced: true`, cannot
be turned off). The section of rule 4 is produced by `uv run python scripts/sandbox_breach_report.py <run dirs>`.

The sandbox code is copied from AdaMAS (see `docs/USING_ADAMAS.md` §2.1); until it is in place, no agent may be
started and no workspace code may be run in this project. Section format of rule 4: `docs/sandbox_breach_log.md`.

## 正对照与额度

- 每次改动流水线（单元执行器、门、修复证据、预算账本、规划者、进化者、诊断、提交检查、沙盒接入），先通过本项目的正对照（`docs/USING_ADAMAS.md` §4 第 13 项），再做任何运行。
- 花额度的运行先向用户说明预计用量，得到同意再跑。提交和推送也先问。
