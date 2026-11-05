# AdaMAS-unit

AdaMAS 多阶段方法的单元化重构：按里程碑串行执行单元，单元之间只传代码；每个单元经过出题者的验收门，至多修复 3 次；
整个仓库共用一本预算账本（token、等待时间、上下文压缩次数、修复次数），由独立的预算规划者审批追加申请。
智能体框架为 mini-swe-agent。

v0.2：一次完整的多阶段执行称为一个轮回；只有预算规划者在轮回之间进化（预算表、通用规则、轮回摘要），
考卷只出一次并冻结，经过验证的规则晋升为 harness 机制。

- 设计（v0.2，合成稿）：[docs/DESIGN.md](docs/DESIGN.md)
- 用户的设计扩展原文：[docs/AdaMAS-unit：可进化的预算规划者（设计扩展 v0.2）.md](<docs/AdaMAS-unit：可进化的预算规划者（设计扩展 v0.2）.md>)
- 如何使用现有框架（AdaMAS、mini-swe-agent）：[docs/USING_ADAMAS.md](docs/USING_ADAMAS.md)
- 沙盒越界警告格式：[docs/sandbox_breach_log.md](docs/sandbox_breach_log.md)

## 状态（2026-11-05）

轮回之内的流水线已经写完，正对照通过；尚未做任何花额度的运行。轮回之间只写了机械部分（预算表校验、接受规则、规划者状态）。

| 模块 | 文件 |
|---|---|
| 沙盒（复制自 AdaMAS 34276d69）、越界扫描（加了 mini-swe-agent 命令记录入口、规划者状态金丝雀） | `src/orchestra/sandbox/` |
| 出题审计、文档名称提取（复制）、测试工具包（复制） | `src/orchestra/codeprojecteval/`、`src/orchestra/harness/testkit/` |
| 题目与单元、工作区 | `src/adamas_unit/task.py` |
| 预算账本 | `src/adamas_unit/ledger.py` |
| 带预算的智能体（mini-swe-agent 子类）、沙盒环境 | `src/adamas_unit/agent.py`、`sandbox_env.py` |
| 模型通道、压缩摘要 | `src/adamas_unit/model.py` |
| 会话包装、角色提示 | `src/adamas_unit/sessions.py`、`configs/roles/` |
| 验收门（逐条结果、三栏） | `src/adamas_unit/gate.py`、`gate_plugin.py` |
| 预算规划者（四类决定、固定规则兜底） | `src/adamas_unit/planner.py` |
| 进展测量（快照副本、harness 开销） | `src/adamas_unit/progress.py` |
| 出题阶段（审计、修卷、空洞用例、冻结与哈希） | `src/adamas_unit/authoring.py` |
| 提交检查 | `src/adamas_unit/submit_check.py` |
| 单元执行器与单轮驱动 | `src/adamas_unit/round.py` |
| 规划者状态（预算表、规则、轮回摘要；带金丝雀） | `src/adamas_unit/planner_state.py` |
| 进化者的机械部分（校验、接受规则） | `src/adamas_unit/evolver.py` |

    uv run python scripts/sandbox_selfcheck.py      # 沙盒自检，11 项
    uv run python scripts/positive_control.py       # 正对照，33 项，不调用模型
    uv run --group dev pytest -q tests              # 单元测试
    uv run python scripts/author_suites.py ...      # 出题阶段（花额度）
    uv run python scripts/run_round.py ...          # 一个轮回（花额度）

尚未写：诊断（DESIGN §8.4 的八项）、进化者的提议调用、单元重放、密封评分（E2 才用）、E0 的批量驱动。
