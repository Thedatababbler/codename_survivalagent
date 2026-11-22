# ContextBench 接入说明（步骤 A，2026-11-22）

本文记录 ContextBench 的来源、许可证、固定版本、生成参数、评分方式、确定性检查结果，以及它如何接到 v0.3 的执行者上。
步骤 A 只做核实和设计，没有改动 `src/`，没有启动智能体，没有花额度。

## 1. 来源、许可证、固定版本

| 项 | 内容 |
|---|---|
| 仓库 | https://github.com/RulinShao/ContextBench |
| 本机位置 | `/root/reference/ContextBench`（项目目录之外，只读使用，不修改其中任何文件） |
| 固定提交 | `fb533c18c78a82d3f1b2047a9c1700735cf829ae`（2026-10-10，"Record each turn before a harness compacts it"） |
| 许可证 | Apache-2.0（`LICENSE`、`pyproject.toml`）。允许研究使用、修改和运行。 |
| 论文 | Context Language Models，arXiv:2609.37725 |
| CLM 接入层 | `/root/reference/context-language-models/clm/clm_contextbench/`，CC BY-NC 4.0，只借鉴机制，不复制代码 |

使用时把 `/root/reference/ContextBench` 加入解释器路径，并设 `PYTHONDONTWRITEBYTECODE=1`，不在检出目录里留下任何文件。
每次运行前核对检出目录的提交号等于上表，且工作区干净；不一致就拒绝运行。

**不依赖 Harbor 或 Docker。** 核心（生成器、协议、评分器、Python 运行器）只需要 `pyyaml`、`numpy`，可选 `tiktoken`
（本项目环境已有 tiktoken 0.14.0，`o200k_base` 可离线加载）。`contextbench/harbor/` 下的 `turn_source.py`、`mixin.py`
也不导入 Harbor；只有 `harbor/agents/`、`wrap.py`、`verifiers.py` 需要它，我们不用。

**注意：检出目录自带答案。** `tasks/` 下有论文的 256 个任务（种子 42–49），每个都带 `tests/gold.json`。这些也公开在
GitHub 和 Hugging Face 上。因此：

- `/root/reference` 必须在智能体的可读路径之外（现有文件规则只放行工作区和系统目录；步骤 B 的正对照要实测一次）；
- 进化集和检验集建议不用种子 42–49，另取种子（生成器对任意整数种子都能生成，已用 1001 验证）。

## 2. 四个任务、生成参数、压力档位

实例由三个文件组成：`chunks.jsonl`（每行一个操作：`index`、`header`、`body`）、`gold.json`（答案）、`meta.json`。
生成入口：`contextbench.tasks.<family>.generate.generate(seed=..., **knobs)` 与 `write_outputs(dir, result)`。
档位表：`contextbench/configs/paper.yaml`（与 `clm_exp/configs/paper.yaml` 逐字节相同）。

论文设置：上下文预算 32,768，预留 2,048，可用 30,720；种子 42–49。
压力 P = 流中所有操作正文的 token 数 ÷ 预算（`o200k_base`）。

| 任务（family） | 流 | 档位旋钮 | 固定参数 | 论文档位（压力） |
|---|---|---|---|---|
| 存取键值 `kv_store` | 若干批 `SET key = value`，最后 24 条 `GET` | `records` | 每批 100 条，值 24 个随机词，24 条查询 | R130 (0.2)、R260 (0.4)、R520 (0.8)、R1000 (1.5)、R2000 (3.0)、R4000 (6.0)、R8000 (12.0)、R16000 (23.9) |
| 日志查询 `log_triage` | 若干批日志行，最后 24 个问题（计数或查找） | `lines`、`lines_per_batch` | 24 条查询 | L160 (0.2)、L320 (0.4)、L640 (0.9)、L1600 (2.2)、L3200 (4.1)、L6400 (7.9)、L12800 (15.7) |
| 保留关键行 `needle_retention` | 每块几行关键行加约 4K token 的填充 | `chunks`、`needles_per_chunk` | 每块 140 行填充，每行 10 词 | K4-C4 (0.5)、K4-C8 (1.0)、K4-C24 (2.9)、K8-C48 (6.0)、K8-C60 (7.5)、K4-C96 (11.7)、K2-C192 (23.0)、K2-C224 (26.8) |
| 编辑数独棋盘 `sudoku_sketchpad` | 每轮一步棋，智能体重新打印整个棋盘 | `turns` | 16×16 棋盘，90 个已知格，1 个棋盘 | t4、t8、t15 (0.9)、t30 (1.7)、t60 (3.2)、t100 (5.3)、t150 (8.0) |

数独的操作正文很短（每步约 59 token），它的压力来自每步重新打印的棋盘（约 4K token）；括号里是论文附录的口径
（每步计一次棋盘）。**数独在论文档位里最高只有 8 倍，没有 12 倍档。**

本机实测（种子 42，`o200k_base`）：

| 实例 | 操作数 | 流 token | 流 ÷ 32,768 | 最大单个操作 |
|---|---|---|---|---|
| kv_store R130 / R1000 / R4000 | 26 / 34 / 64 | 6,956 / 49,106 / 195,117 | 0.21 / 1.50 / 5.95 | 4,903 / 4,903 / 4,937 |
| log_triage L160 / L1600 / L6400 | 36 / 139 / 143 | 7,461 / 67,630 / 255,438 | 0.23 / 2.06 / 7.80 | 601 / 610 / 2,223 |
| needle_retention K4-C4 / K4-C24 / K4-C96 | 4 / 24 / 96 | 15,784 / 94,938 / 380,358 | 0.48 / 2.90 / 11.61 | 3,976 / 3,997 / 4,019 |
| sudoku_sketchpad t4 / t30 / t100 | 4 / 30 / 100 | 230 / 1,742 / 5,814 | —（见上） | 59 |

**可行性门**（`contextbench.feasibility`，生成时检查）与预算绑定：P > 1；单个操作 ≤ 0.9 × 可用 ÷ 5（30,720 时为 5,529）；
理想策略必须保留的量 ≤ 0.9 × 可用 ÷ 2；至少 4 个操作。键值任务每批约 4,900 token，所以**预算若低于约 27K，
论文档位的键值任务就过不了"单个操作"这一条**，只能改 `records_per_chunk` 旋钮（不再是论文档位）。

## 3. 评分方式

评分入口：`contextbench.graders.grade_run(run_dir, gold)`，按 `gold["meta"]["task_family"]` 选评分器。
评分只读智能体的上下文，不读磁盘。读取顺序：`context_snapshots/turn-*.json`（论文口径）→ `trajectory.json` → 最终报告；
实际读的是哪一个写在结果的 `graded_from` 里。我们只用第一种，结果里 `graded_from` 不是 `snapshots` 的运行视为评分异常。

| 任务 | 得分 | 读哪些快照 |
|---|---|---|
| `kv_store` | 24 条 GET 中答对的比例。答案必须在 `<<<ANSWER key=K>>> … <<<ANSWER END>>>` 块里，值逐字符一致（空白归一） | **所有**快照：答案块在任一快照出现过即算 |
| `log_triage` | 24 个问题中答对的比例。`<<<ANSWER qid=N>>> … <<<ANSWER END>>>`；查找题要求原消息文本为子串，计数题要求答案里恰好一个数字且相等 | 所有快照 |
| `needle_retention` | 关键行逐字出现的比例 | **只读最后一个快照**（运行结束时还留在上下文里的内容） |
| `sudoku_sketchpad` | 每一步之后的正确棋盘中，智能体展示过的比例（`<<<SKETCHPAD BEGIN>>>` 与 `END` 之间，逐格一致） | 所有快照 |

得分都在 0–1 之间。`grader_valid` 为 0 表示无法评分，不当作 0 分处理。

运行的结束方式（`terminated_by`）：`submit`、`stream_exhausted`（两者都是正常结束）、`budget_wall`（将要发送的上下文
超过 预算 − 预留，按 `o200k_base` 计）、`timeout`、`max_steps`。

## 4. 确定性检查与自检（2026-11-22，本机）

- 四个任务 × 3 个档位 × 3 个种子（42、43、1001）共 36 个实例，在两个独立进程里各生成一次（`PYTHONHASHSEED` 不同），
  108 个文件的 SHA-256 全部相同。
- 抽 4 个实例（kv R1000 种子 42、log L1600 种子 43、needle K4-C24 种子 42、sudoku t30 种子 42）与检出目录
  `tasks/` 里论文发布的 `chunks.jsonl`、`gold.json` 比较：逐字节相同。即本机生成的就是论文的实例。
- 用 ContextBench 自带的标准答案智能体（`OracleAgent`，读答案，只用于自检，不是基线）经 Python 运行器跑 8 个实例
  （每个任务一个低压力档、一个超窗档）：得分全部 1.0，`graded_from = snapshots`，正常结束。说明不用 Docker，
  生成、送达协议、快照、评分这一整条链在本机可用。
- 临时生成的实例和运行目录放在 `outputs/` 下的一个 700 目录里，检查完已删除；`/tmp` 里只留了哈希和计数。

## 5. 映射：ContextBench 如何接到 v0.3 执行者

### 5.1 用它的哪些部件（都不改）

| 用途 | 部件 |
|---|---|
| 生成实例 | `contextbench.tasks.<family>.generate`（`generate`、`write_outputs`）、`contextbench.cli.resolve_level` |
| 任务说明 | `contextbench.tasks.scaffold.instruction(family, delivery="bash", initial_block=…)`（论文原文，不含任何方法提示） |
| 送达、就绪判定、拒绝提前提交、结束原因 | `contextbench.harbor.turn_source.TurnSource.from_records(...)`（不依赖 Harbor） |
| 写快照 | `contextbench.tokens.record_snapshot(logs_dir, step, messages, extra)` |
| 评分 | `contextbench.graders.grade_run(run_dir, gold)` |

我们自己写的只有"驱动"：一个新的执行者子类和一个运行脚本，放在 `src/survival/`（新文件，如 `cb_agent.py`、`cb_run.py`），
不改 v0.2/v0.3 现有的仓库任务路径。

### 5.2 目录与密封

```
<密封目录>/contextbench/<family>-<level>-seed<N>/   chunks.jsonl  gold.json  meta.json      只有驱动（root）能读
outputs/cb/<run>/                                     运行目录，智能体读不到
    agent/context_snapshots/turn-NNNN.json            快照（驱动写）
    agent/interactive_stream.json                     TurnSource 写：送达了几条、怎么结束
    result.json                                       得分、用量、压缩次数、峰值上下文、回滚次数
<工作区>/                                             归 agent_uid 所有；开始时为空，只有智能体自己写的文件和记忆文件
```

- 操作流**不进工作区**。驱动在自己的进程里读 `chunks.jsonl`，交给 `TurnSource`；智能体的 shell 里没有流文件，
  无法提前读（Harbor 路径是读进来再从容器里删掉，效果相同）。
- `gold.json` 只在运行结束后由驱动读入并传给 `grade_run`。评分器只做正则和字符串比较，不执行智能体写的任何代码，
  所以评分在驱动进程里进行，不需要 `scoring_uid` 那一套。
- 密封目录的位置待定（见第 6 节第 1 项），要求：在工作区之外、不在 `/tmp`、不进 git。

### 5.3 一次运行的流程

1. 驱动建工作区、启动沙盒里的环境（与 v0.3 相同：文件规则 + 网络拦截 + 切到 `agent_uid`）。
2. 消息列表初始为：系统提示词（v0.3 执行者的，结尾换成 ContextBench 的 `MARKER_PARAGRAPH`：一次一个操作，
   用 `echo READY_FOR_NEXT_OP` 取下一个，不提交）→ 用户消息：任务说明 → 用户消息：第 0 个操作（`first_turn()`）。
3. 循环（v0.3 的 `query()` → `execute_actions()`）：
   - 模型回复一条 bash 命令；管理命令（`compact` 等）照旧由框架处理，不计步数。
   - 命令在沙盒里执行，得到退出码和输出，作为观察消息加入上下文（末尾带资源读数）。
   - 驱动调用 `source.pending(command, exit_code)`，把返回的文本各作为一条**用户消息**追加在观察消息之后：
     下一个操作、"提前提交被拒"的说明，或"标记不是独立命令"的说明。操作只经这一条路进入上下文。
   - 没有工具调用的回复：沿用 v0.3 的格式错误提示，文字换成 ContextBench 的"只有命令能推进"。
   - `source.done` 为真（流走完后再次就绪，或提交）时结束会话。
4. 结束后驱动写最后一个快照，读 `gold.json` 评分，写 `result.json`。

阶段、`stage_done`、追加预算、审阅、子智能体在这里都不存在：整条流算一个阶段，由流的结束来结束。
这三个 skill（审阅修复调度、追加审批、完成检查）装载 π_0 版本但永不触发。

### 5.4 快照：何时写、写什么（评分器直接可用）

格式完全由 `record_snapshot` 产生，我们不自己拼文件。它写
`<run>/agent/context_snapshots/turn-NNNN.json`，内容为
`step`、`tokens`（`o200k_base`）、`token_method`、`n_messages`、`kind`、`n_ops_delivered`、`n_ops_total`、`budget`、`reserve`、
最后是 `messages`。评分器从 `<run>/agent/context_snapshots` 找到它们，按 `turn-` 后的数字排序。

`messages` 是当时上下文的原样列表，每条只保留模型实际看到的字段：`role`、`content`，以及有工具调用时的
`tool_calls`（mini-swe-agent 的 `extra` 等内部字段去掉）。评分器只扫 `content` 里的文字。

时机与 `CBStreamMixin` / `ContextBenchAgent` 一致，编号从 0 递增：

| 时机 | `kind` | v0.3 里的位置 | 为什么 |
|---|---|---|---|
| 每次模型调用之前，对将要发送的消息列表 | `sent` | `ManagedAgent.query()` 中，`before_call()` 和溢出处理之后、`self.model.query(self.messages)` 之前 | 论文口径：评分看的是模型实际收到的上下文 |
| 任何会改写消息列表的动作之前，对改写前的列表 | `turn` | `compact()`（框架压缩、强制压缩、智能体自己 `compact`）、`ContextWindow.roll_back()`、"无管理"组的截断，各自动手之前；列表没变就不写 | 本轮的工具输出刚到就被压掉时，答案仍然被记下（ContextBench 提交 fb533c1 的规则） |
| 会话结束时，对最终的消息列表 | `final` | 驱动的收尾处，任何情况下都写（包括异常结束） | `needle_retention` 只读最后一个快照 |

摘要用的那次模型调用（框架压缩时另起的一次请求）不写快照，也不受预算墙约束，但 token 照常记账——与 ContextBench 的规定相同。

"答案必须出现在上下文里才算"由此自动成立：记忆文件里的内容只有被智能体 `cat` 或 `printf` 出来、进了某条消息，
才会出现在快照里。压缩后用记忆文件重建上下文时，记忆文件的内容进了消息列表，也就进了下一个 `sent` 快照——
这对 `needle_retention` 是合规的保留方式，对另外三个任务没有额外好处（答案块仍须智能体自己打印）。

### 5.5 预算墙与窗口

- ContextBench 的墙：将要发送的上下文超过 预算 − 预留（`o200k_base`）就结束，记 `budget_wall`。
- v0.3 的窗口：发送前本地计数，超窗时先回滚最近几轮并要求压缩，不直接结束。
- 接法：v0.3 的窗口设为 预算 − 预留，并且**在这个设置里用未经校准的 `o200k_base` 计数**（v0.3 现在会按服务端
  报告的 token 数校准比例），这样"上下文占比"读数、触发条件和 ContextBench 的墙是同一把尺。超窗请求永远不会发出，
  所以正常情况下不会出现 `budget_wall`；真到了回滚也救不回来的地步（v0.3 的 `ContextExhausted`、`RollbackLimit`），
  记为 `budget_wall`。
- 这与论文的 CLM 设置不同（论文关闭了回滚重试和所有提醒，超限后只给最后一轮）。**因此我们的数字不能与论文图表里的数字直接比较**，
  只在我们自己的三个组之间比较。

### 5.6 三个组在这里各是什么

| 组 | 实现 |
|---|---|
| π_0 | `memory-compaction`：`context >= 0.75` → `framework_compaction`（另一次模型调用写摘要，保留最近 6 轮）；`token-reminder`：`context >= 0.90` → `remind` 一次；`subagent-handoff`：不交接 |
| 无管理 | 不装载这三个 skill 的触发条件；上下文将超窗时由驱动从最早的消息开始丢弃（保留系统提示词和任务说明），不总结、不提醒 |
| 进化后 | 前沿里选出的 skill 组合 |

资源读数、回滚三组相同。现有 π_0 的 `token-reminder` 触发条件是 `stage_tokens >= 0.90`（阶段额度），这里要另起一套
ContextBench 用的 π_0 文档（如 `configs/cb_skills_pi0/`），不改 `memory/planner/skills/` 里仓库任务的 π_0。

### 5.7 进化者看到什么

每次运行的数字摘要：任务、档位、种子编号、得分及分项（`reward_lookup`、`reward_count` 等）、`terminated_by`、
送达操作数 ÷ 总数、token 用量、压缩次数、回滚次数、峰值上下文、上下文占比随操作序号变化的轨迹、各触发条件触发的时刻。
不含操作正文、智能体的回复、答案。

## 6. 用户决定（2026-11-22）

1. 窗口 32,768（可用 30,720），只用压力大于 1 的档位；数独最高档用 8 倍（t150）。在用的档位列在 `configs/contextbench.yaml`。
2. 种子取 1000 以上，进化集与检验集不重叠；42–49 永不使用（代码拒绝）。
3. 密封目录 `sealed/contextbench/`（0700，不进 git）。智能体不能读取、也不能运行 `/root/reference/ContextBench`；
   智能体看不到当前实例的种子（任务说明、目录名、文件名、环境变量里都不出现；运行目录名是随机的）。
4. 触发条件与超窗判定用未校准的 `o200k_base`（ContextBench 自己的计数函数）；记账与成本用服务端报告的 token 数。
5. 结果只在三组之间比较，与论文数字不可比。
6. `subagent-handoff` 冻结为"不交接"。π_0 的 90% 提醒照原定义。

## 7. ContextBench 对使用文件系统的规定（核实结果）

依据都在固定提交的检出目录里：

| 任务 | 规定 | 依据 |
|---|---|---|
| `kv_store`、`log_triage` | **允许**把磁盘当草稿和存储（把一批内容写进文件，之后 `grep`）。但"找到"不算作答：答案必须打印成 ANSWER 块出现在上下文里。 | 任务说明原文（`tasks/<family>/scaffold.md`，"You MAY use the disk as scratch/storage, but FINDING a value … is NOT answering"）；可行性检查把"每批都写到磁盘、上下文里只留索引"当作理想策略（`feasibility.py`）；批次的起始标记本身写着 "offload this whole block this turn" |
| `needle_retention` | 任务说明要求**不要写磁盘**，答案只从最终上下文读取。 | `scaffold.md`："There is no answer file; do not write to disk (disk is ignored by the grader)" |
| `sudoku_sketchpad` | 任务说明要求**不要把棋盘写进文件**，评分只读上下文。 | `scaffold.md`："Grading reads your context window, not disk — do not write the board to a file" |

- **没有任何强制手段。** 它自带的智能体（`harbor/agents/base.py`）给模型一个不受限的 `bash` 工具，磁盘状态在命令之间保留；
  唯一被拿走的是操作流文件本身（读入后从容器里删除，智能体不能提前读）。后两个任务的"不要写磁盘"只是任务说明里的一句话，
  评分器的做法是"磁盘上的东西不看"。
- **操作只存在于对话里。** 要把一批内容存盘，智能体必须自己在命令里把它重新写一遍（论文各方法的说明都是用 heredoc 整批粘贴）。
- **读回上下文的内容算数。** 评分器只看快照里的文字，不区分来源。实测（脚本模型）：`needle_retention` 里把关键行存盘、
  最后一轮 `cat` 出来，得 1.0；只存盘不打印，只有最后几块的关键行，得 0.29。

我们的实现与之一致：工作区在命令之间保留，智能体可以自由读写；任务说明用原文、不改；不加任何额外的禁止或检查；操作流不进工作区。

## 8. 步骤 B 的实现（2026-11-22）

| 文件 | 作用 |
|---|---|
| `configs/contextbench.yaml` | 检出位置、固定提交、预算、密封目录、种子与档位的限制 |
| `src/survival/cb.py` | 唯一导入 `contextbench` 的地方：核对提交号与工作区干净、生成实例、送达对象、计数、快照、评分 |
| `src/survival/cb_agent.py` | `StreamAgent`：v0.3 的 `ManagedAgent` 加上操作流、快照、三个组 |
| `src/survival/cb_run.py` | 一次运行：沙盒环境、账本、规划者、评分、`result.json`、`digest.json` |
| `configs/cb_skills_pi0/` | 这个设置的 π_0：75% 总结、90% 提醒一次、不交接；另外三个 skill 是仓库任务 π_0 的原样拷贝，永不触发 |
| `configs/roles_cb/` | 系统提示词（结尾是 ContextBench 的标记说明原文）、总结用的提示词 |
| `scripts/cb_run.py` | 运行入口（花额度） |
| `scripts/positive_control_cb.py` | 正对照（不花额度），32 项 |

`src/survival/agents.py` 只加了三个默认为空的钩子（发送前、改写前、无工具调用时的提示文字），仓库任务的行为不变，v0.3 正对照 64/64。

与第 5 节设计相比，实现时定下的几点：

- **每条回复只执行第一个工具调用**（与 ContextBench 自带智能体相同），保证一条回复最多推进一个操作。
- **回滚不丢操作。** v0.3 的回滚会撤销最新的几条消息；已送达的操作不可能再送一次，所以回滚后把它放回上下文。
  放回后仍超窗：有 skill 的组强制压缩，"无管理"组丢最早的消息；都不行才以 `budget_wall` 结束。
- **"无管理"组**：没有规划者、没有 `compact` 命令、没有提醒；下一次请求放不下时，从最早的消息开始丢
  （保留系统提示词和任务说明，工具结果不与它的调用分开）。
- **记忆文件**：只有带 skill 的组有 `AGENT_MEMORY.md`，没有固定标题；智能体在上次压缩之后写过它，`compact` 就用它重建上下文，否则用总结。
- **总结用的模型调用**看到任务说明和被替换的那段对话，提示词是通用的（`configs/roles_cb/summary.md`），不含任何任务策略。
- **压缩次数的上限**设为 400（只是安全上限）。仓库任务的默认值 8 在这里一个高档位运行就会用完。
- **非流结束的会话**记成 ContextBench 的结束原因：上下文无法挽回 → `budget_wall`；用量上限、步数上限、连续 10 次无工具调用 → `max_steps`。
  v0.3 自己的退出状态另存在 `exit_status`。

一个需要注意的现有行为：规划者的触发条件是"由假变真时触发一次"。π_0 在 75% 压缩时保留最近 6 轮；
键值任务里 6 轮就是 6 批（约 29K），压缩之后上下文仍在 75% 以上，这条触发就不会再次触发，直到上下文超窗、回滚、强制压缩。
脚本模型在 R1000 上的实测：2 次触发的压缩、2 次回滚、2 次提醒（都在 100% 处）。这是 v0.3 现有的规则，没有改。
