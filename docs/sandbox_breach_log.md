# Sandbox breach log

Since 2026-10-11, every run records what its agents tried outside the sandbox. This page defines the records, the
levels, what follows from them, and the section every task report carries. Code: `src/orchestra/sandbox/breach.py`;
report tool: `scripts/sandbox_breach_report.py`.

## Where

- `<run_dir>/sandbox_breach.log`: one JSON line per event of that run.
- `outputs/sandbox/breach_index.jsonl`: the same lines from all runs (append-only).
- `outputs/sandbox/void_runs.jsonl` and `<run_dir>/SANDBOX_VOID`: runs voided by a CRITICAL event.
- `summary.json` → `sandbox_breach`: the run's counts, its CRITICAL events, `void` and `manual_review`.

The scan runs at the end of every run (`run_codeprojecteval_decomp`, the mas-baselines Codex runner). It reads every
command an agent ran with its output: the captured turn items, or the Codex session log in the agent's private
`CODEX_HOME` when items are missing.

## Events and levels

| event | what | level |
|---|---|---|
| `read_denied` | an attempt to read a denied path, refused by Landlock (the command failed with a permission error) | WARNING |
| `list_denied` | listing or searching a denied path (`find /`, `ls` on a denied directory, `rg`/`grep -r` there) | WARNING |
| `network_denied` | an external connection, refused by the network guard | WARNING |
| `package_fetch` | `pip install`, `pip download`, `curl`, `wget`, a remote `git clone` | WARNING |
| `ld_preload_tamper` | unsetting, overriding or exporting `LD_PRELOAD` | CRITICAL |
| `sandbox_tamper` | touching the sandbox configuration, its variables (`ADAMAS_SANDBOX*`) or launch arguments (`--policy`, the launcher, `landlock`, `unshare`, ...) | CRITICAL |
| `canary` | a memory canary in any agent input, output, record or workspace file | CRITICAL |
| `capture_empty` | an agent call with neither turn items nor a session log: it cannot be audited | CRITICAL |
| `unblocked_breach` | an out-of-bounds command the audit finds and nothing refused (a read of a denied path that succeeded; an external connection that was not refused) | CRITICAL |
| `original_project_search` | outside the workspace, searching, listing, finding files or importing by the name of the task's original project or of its modules: a listing / search with an absolute path outside the run, a query of installed distributions (`pip show/download/install <name>`, `pip list \| grep <name>`, `importlib.metadata`, `pkg_resources`, `find_spec`), an import run from a directory outside the run or with `-I`/`-S`. Not counted: importing or checking the package being written (in the workspace, or a copy the same command built, installed with `--target` or put on `PYTHONPATH`), and the agent's own scratch areas (`/tmp`, `$TMPDIR`). Names: the project, its package and its distinctive module stems from the frozen plan (user decision 2026-10-21) | CRITICAL |

## A record

```json
{"time": "2026-10-11T08:00:00Z", "run_id": "<batch>/<task>", "role": "agent_3_fixer_gate_repairer",
 "event": "read_denied", "level": "WARNING", "target": "/root/projects/AdaMAS/memory",
 "command": "/bin/bash -lc 'cat /root/projects/AdaMAS/memory/VERSION'"}
```

- `target` is a directory, never a file name or file content.
- `command` is cut to 200 characters.
- `role` is the agent node (its trace directory).

## What follows

1. **CRITICAL**: the run is void.
   - Its results count in no statistic: the ledger reader `orchestra.control.evolution.ledger.read_jsonl` and the memory transfer channel `runs_of` drop it.
   - The task report says so at the top.
2. **WARNING**: the run's results stand.
   - Every warning is listed in the report.
   - A run with 5 or more warnings is marked for manual review (`manual_review: true`).

## The report section

Every task report ends with this section, even when every count is 0:

```
## 沙盒越界警告
- 运行数：N；命令总数：M
- WARNING：x 条（按事件类型分列）
- CRITICAL：y 条（逐条列出：运行 id、角色、事件、命令摘要）
- 结论：无越界 / 有越界（处理方式）
```

To generate it:

```
uv run python scripts/sandbox_breach_report.py <run_dir> [<run_dir> ...]
uv run python scripts/sandbox_breach_report.py --glob 'outputs/<batch>/*/*'
```

Any CRITICAL event, and any run marked for manual review, goes at the top of the report before other results.
