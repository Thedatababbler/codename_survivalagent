# Copied from AdaMAS src/orchestra/sandbox/policy.py (34276d69) for AdaMAS-unit, 2026-11-05. Changed: default_probes also probe the
# AdaMAS and AdaMAS-single trees (their outputs hold generated code and copies of earlier runs).
"""Sandbox policies, the network guard and the per-run manifest (sandbox spec A, 2026-10-09).

Mechanism, chosen because the host allows nothing else (no namespaces, no
CAP_SYS_ADMIN / CAP_NET_ADMIN):

1. **Landlock filesystem** (``landlock.apply``). An agent process and every
   command it runs read only:
   - its workspace and that workspace's git directories;
   - a private home / tmp / CODEX_HOME;
   - its task's own virtual environment and the Python installs it links to;
   - the Codex binary;
   - system trees (``/usr``, ``/bin``, ``/lib*``, ``/etc``, ``/proc``,
     ``/sys``, ``/dev``).

   Everything else fails with EACCES before the read. That covers memory/, the
   dataset (held-out and reference), the verification suites, other runs,
   outputs/, configs/, the repository source, ``/tmp`` and ``~/.codex``.
2. **Landlock TCP**: ``connect()`` only to the model proxy's port and the local
   ephemeral range (32768-60999). External services on their ports (80, 443,
   22, 9418, ...) are unreachable for every program, static binaries included.
3. **netguard** (``LD_PRELOAD``): every dynamically linked program (bash,
   python, git, curl, pip) can connect / sendto / sendmsg only to loopback or
   AF_UNIX addresses, on any port.
4. The after-the-fact audit of the captured turn items stays as a second line.

Gate tests run under the same filesystem rules (workspace, the run's harness
directory with the gate suites and the testkit, the task venv) and the same
network rules. Held-out scoring keeps its file access and gets the network
rules only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from orchestra.sandbox.landlock import Policy, abi_version, port_ranges

ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "configs" / "sandbox.yaml"
ENFORCED_ENV = "ADAMAS_SANDBOX_ENFORCED"
VENV_ENV = "ADAMAS_SANDBOX_VENV"
NETGUARD_SRC = Path(__file__).resolve().parent / "native" / "netguard.c"
NETGUARD_DIR = Path("/usr/local/lib/adamas")
SYSTEM_READ = ["/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/proc", "/sys", "/.uv/python_install"]
DEV_WRITE = ["/dev"]
EPHEMERAL = (32768, 60999)


# Paths no confined policy may grant (user decision 2026-10-11): copies of task projects found by the machine-wide
# sweep stay on disk and the sandbox keeps them out. Landlock only allows, so this list is enforced by refusing
# any policy that grants one of them, or a directory above one (``check_explicit_deny``), and probed by S1.
SEALED_ARCHIVE_DIR = Path("/root/.adamas_sealed")
EXPLICIT_DENY = (
    "/usr/local/lib/python3.12/dist-packages",       # system Python packages (PyJWT, flask, dotenv, pathspec, portalocker)
    "/usr/lib/python3/dist-packages",
    "/usr/share/python-wheels",
    "/.uv/python_install/*/lib/python*/site-packages",  # uv-managed interpreters' own packages
    "/venv",                                         # /venv/mas, /venv/medskill, /venv/livecodebench, ... (other environments)
    "/opt/portal-aio",
    "/opt/miniforge3",
    "/root/projects/mas-baselines",
    "/root/projects/EvoMAS",
    "/root/projects/Mini-Agent",
    "/root/codex-benchmarks/realbench_codex_vanilla",  # realbench data
    str(ROOT / "data" / "realbench"),
    "/root/data/.dataproc",
    "/root/codex-benchmarks/nl2repo/upstream",       # moved to SEALED_ARCHIVE_DIR on 2026-10-11; kept here should it return
    str(SEALED_ARCHIVE_DIR),
)


def explicit_deny_paths() -> list[str]:
    import glob as _glob

    out: list[str] = []
    for d in EXPLICIT_DENY:
        out += sorted(_glob.glob(d)) if any(c in d for c in "*?[") else [d]
    return out


def check_explicit_deny(policy: Policy) -> None:
    """Raise when a filesystem-restricting policy grants a denied path or a directory that contains one."""
    if not policy.restrict_fs:
        return
    deny = [os.path.realpath(d) for d in explicit_deny_paths()]
    for p in [*policy.read, *policy.write]:
        rp = os.path.realpath(p)
        for d in deny:
            if rp == d or rp.startswith(d + "/") or d.startswith(rp.rstrip("/") + "/"):
                raise SandboxRequiredError(f"sandbox policy grants {p}, which is or contains the denied path {d}")


BLOCKED_DIR_NAMES = ("dist-packages", "site-packages", "python-wheels")
# ensurepip/_bundled holds pip wheels, and pip vendors task projects (tenacity): blocked like a package dir
BLOCKED_SUBDIRS = (("ensurepip", "_bundled"),)
_EXPANDED: dict[tuple[str, ...], list[str]] = {}
_LISTED: dict[tuple[str, ...], list[str]] = {}


def without_package_dirs(roots: list[str]) -> list[str]:
    """``roots`` expanded so that no system ``dist-packages`` / ``site-packages`` is readable (2026-10-10).

    Third-party packages installed for the system or uv Pythons may include a
    task's own project (PyJWT sits in /usr/local/lib/python3.12/dist-packages).
    Landlock only allows, it cannot carve a hole, so a root that contains a
    blocked directory is replaced by its children, recursively, minus the
    blocked ones. Only the task's own venv keeps its site-packages.
    """
    key = tuple(roots)
    if key in _EXPANDED:
        return _EXPANDED[key]
    blocked: set[str] = set()
    links: set[str] = set()        # symlinks whose target is or holds a blocked directory: their parent must split
    seen_real: set[str] = set()
    listed: set[str] = set()
    holds_cache: dict[str, bool] = {}

    def _holds_blocked(real: str, depth: int = 0) -> bool:
        if real in holds_cache:
            return holds_cache[real]
        holds_cache[real] = False      # cycle guard
        found = any(part in BLOCKED_DIR_NAMES for part in Path(real).parts)
        if not found and os.path.isdir(real) and depth < 8:
            for dirpath, dirnames, filenames in os.walk(real, followlinks=False):
                if any(d in BLOCKED_DIR_NAMES for d in dirnames) or any(
                        os.path.islink(os.path.join(dirpath, x)) and _holds_blocked(os.path.realpath(os.path.join(dirpath, x)), depth + 1)
                        for x in dirnames + filenames):
                    found = True
                    break
        holds_cache[real] = found
        return found

    def _collect_blocked(r: str) -> None:
        if not os.path.isdir(r):
            return
        for dirpath, dirnames, filenames in os.walk(r, followlinks=False):
            if dirpath.startswith(("/proc", "/sys")):
                dirnames[:] = []
                continue
            for d in list(dirnames):
                if d in BLOCKED_DIR_NAMES or (os.path.basename(dirpath), d) in BLOCKED_SUBDIRS:
                    blocked.add(os.path.join(dirpath, d))
                    dirnames.remove(d)
            # Landlock follows links: one pointing into (or at) a package tree must not be granted with its parent
            for x in dirnames + filenames:
                full = os.path.join(dirpath, x)
                if os.path.islink(full) and _holds_blocked(os.path.realpath(full)):
                    links.add(full)

    for r in roots:
        _collect_blocked(os.path.realpath(r))

    def expand(p: str, depth: int = 0) -> list[str]:
        if os.path.islink(p):
            # Landlock follows the link: judge (and expand) the target, so a link cannot open a blocked tree
            real = os.path.realpath(p)
            if depth > 8 or not os.path.exists(real) or any(part in BLOCKED_DIR_NAMES for part in Path(real).parts):
                return []
            if real not in seen_real:
                seen_real.add(real)
                _collect_blocked(real)
                return expand(real, depth + 1)
            return []
        if p in blocked:
            return []
        if not any(b.startswith(p.rstrip("/") + "/") for b in blocked | links):
            return [p]
        try:
            children = sorted(os.listdir(p))
        except OSError:
            return []
        # a split directory keeps readdir only when it is an interpreter's stdlib directory (os.py): Python's import
        # scan needs it, and Landlock grants it for the whole tree below, so every other split directory stays
        # unlistable (2026-10-11: /usr/share and /usr/local/lib/python3.12 no longer list their package dirs)
        if os.path.isfile(os.path.join(p, "os.py")):
            listed.add(p)
        out: list[str] = []
        for c in children:
            out += expand(os.path.join(p, c))
        return out

    result: list[str] = []
    for r in roots:
        result += expand(os.path.realpath(r))
    _EXPANDED[key] = sorted(set(result))
    _LISTED[key] = sorted(listed)
    return _EXPANDED[key]


def listed_dirs(roots: list[str]) -> list[str]:
    """Directories without_package_dirs split open: their names stay listable (readdir only)."""
    without_package_dirs(roots)
    return _LISTED.get(tuple(roots), [])


def load_config(experiment: dict[str, Any] | None = None) -> dict[str, Any]:
    base = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.is_file() else {}
    cfg = {k: dict(v or {}) for k, v in (base or {}).items()}
    for k in ("sandbox", "author"):
        cfg.setdefault(k, {}).update(((experiment or {}).get(k) or {}) if isinstance((experiment or {}).get(k), dict) else {})
    # sandbox-only rule (2026-10-11): neither an experiment yaml nor the environment can turn the sandbox off
    if (experiment or {}).get("sandbox", {}) and (experiment or {})["sandbox"].get("enforced") is False:
        raise SandboxRequiredError("an experiment config sets sandbox.enforced: false; the sandbox cannot be turned off")
    cfg["sandbox"]["enforced"] = bool((base or {}).get("sandbox", {}).get("enforced"))
    return cfg


class SandboxRequiredError(RuntimeError):
    """An agent or workspace-code run was about to start without the sandbox (sandbox-only rule, 2026-10-11)."""


TEST_UNCONFINED_ENV = "ADAMAS_SANDBOX_TEST_UNCONFINED"


def _test_unconfined() -> bool:
    """The one exemption: inside a pytest process, during a test, when the test suite asked for it
    (``tests/conftest.py``). It covers driver-side runs of code that start no agent (harness tests in tmp
    directories); agent launches ignore it (``guard.require_sandbox``)."""
    import sys as _sys

    return ((os.environ.get(TEST_UNCONFINED_ENV) or "").strip() == "1" and "pytest" in _sys.modules
            and bool(os.environ.get("PYTEST_CURRENT_TEST")))


def enforced() -> bool:
    """Always true (sandbox-only rule, 2026-10-11). ``sandbox.enforced`` must be true in configs/sandbox.yaml,
    and no environment variable can turn it off; ``ADAMAS_SANDBOX_ENFORCED`` is no longer read."""
    if _test_unconfined():
        return False
    if not bool((load_config().get("sandbox") or {}).get("enforced")):
        raise SandboxRequiredError("configs/sandbox.yaml: sandbox.enforced must be true (sandbox-only rule); refusing to run")
    return True


def proxy_port() -> int:
    u = urlparse((os.environ.get("OPENAI_BASE_URL") or "http://127.0.0.1:8317/v1").strip())
    return int(u.port or (443 if u.scheme == "https" else 80))


def ensure_netguard() -> Path:
    """Build libnetguard.so into a system tree every sandboxed process may read."""
    src = NETGUARD_SRC.read_bytes()
    tag = hashlib.sha256(src).hexdigest()[:12]
    out = NETGUARD_DIR / f"libnetguard-{tag}.so"
    if not out.is_file():
        NETGUARD_DIR.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        subprocess.run(["gcc", "-shared", "-fPIC", "-O2", "-o", str(tmp), str(NETGUARD_SRC), "-ldl"], check=True, capture_output=True)
        tmp.rename(out)
    return out


PYTEST_CONFIG_NAMES = ("pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")


def ancestor_configs(workspace: str | Path) -> list[str]:
    """pytest looks for its config in every ancestor of the test path and fails on one it may not open;
    those files (and only those, no directory listing) are readable."""
    out = []
    for d in Path(workspace).resolve().parents:
        for n in PYTEST_CONFIG_NAMES:
            if (d / n).is_file():
                out.append(str(d / n))
    return out


def git_dirs(workspace: Path) -> list[str]:
    """The workspace's git metadata outside it: a worktree's gitdir and the common dir."""
    g = Path(workspace) / ".git"
    out: list[str] = []
    if g.is_file():
        text = g.read_text(encoding="utf-8").strip()
        if text.startswith("gitdir:"):
            gd = Path(text.split(":", 1)[1].strip())
            gd = gd if gd.is_absolute() else (Path(workspace) / gd).resolve()
            out.append(str(gd))
            cd = gd / "commondir"
            if cd.is_file():
                common = Path(cd.read_text(encoding="utf-8").strip())
                out.append(str(common if common.is_absolute() else (gd / common).resolve()))
    return out


def venv_reads(venv: str | Path | None) -> list[str]:
    """The task venv (whole, its own site-packages included) and the interpreter it links to (minus its site-packages)."""
    if not venv:
        return []
    v = Path(venv)
    out = [str(v)]
    py = v / "bin" / "python"
    if py.exists():
        out += without_package_dirs([str(Path(os.path.realpath(py)).parent.parent)])
    return out


def system_reads() -> list[str]:
    return without_package_dirs(SYSTEM_READ)


def list_only(venv: str | Path | None) -> list[str]:
    """readdir-only directories of a policy: the split-open system roots and interpreter tree."""
    out = list(listed_dirs(SYSTEM_READ))
    if venv and (Path(venv) / "bin" / "python").exists():
        out += listed_dirs([str(Path(os.path.realpath(Path(venv) / "bin" / "python")).parent.parent)])
    return sorted(set(out))


def net_ports() -> list[int]:
    return sorted({proxy_port(), *port_ranges([EPHEMERAL])})


def agent_policy(*, workspace: str | Path, private: str | Path, venv: str | Path | None, codex_bin: str | Path | None = None,
                 extra_read: list[str] | None = None) -> Policy:
    read = system_reads() + venv_reads(venv) + [str(ensure_netguard().parent)]
    if codex_bin:
        read.append(str(Path(os.path.realpath(codex_bin)).parent))
    read += list(extra_read or []) + ancestor_configs(workspace)
    # a repairer's prompt points at ../repair_evidence (fast_loop.repair_evidence): readable, not writable
    evidence = Path(workspace).parent / "repair_evidence"
    if evidence.is_dir():
        read.append(str(evidence))
    write = [str(workspace), *git_dirs(Path(workspace)), str(private), *DEV_WRITE]
    return Policy(read=read, write=write, connect_ports=net_ports(), list_dirs=list_only(venv))


def gate_policy(*, workspace: str | Path, private: str | Path, venv: str | Path | None, harness_dir: str | Path,
                testkit_dir: str | Path | None = None) -> Policy:
    read = system_reads() + venv_reads(venv) + [str(harness_dir), str(ensure_netguard().parent)] + ancestor_configs(workspace)
    if testkit_dir:
        read.append(str(testkit_dir))
    return Policy(read=read, write=[str(workspace), *git_dirs(Path(workspace)), str(private), *DEV_WRITE], connect_ports=net_ports(),
                  list_dirs=list_only(venv))


def scoring_policy() -> Policy:
    """Held-out scoring: file access unchanged (no filesystem rules), network as for agents."""
    return Policy(read=[], write=[], connect_ports=net_ports(), restrict_net=True, restrict_fs=False)


def confine_scoring(command: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str], Path | None]:
    """Held-out scoring under the network rules only (sandbox spec A §1); file access as before."""
    if not enforced():
        return command, env, None
    import tempfile

    private = Path(tempfile.mkdtemp(prefix="adamas-score-"))
    pol_path = write_policy(scoring_policy(), private / "policy.json")
    out = launcher_env(dict(env))
    out["LD_PRELOAD"] = str(ensure_netguard())
    full = [*launcher(pol_path), *command]
    from orchestra.sandbox.guard import assert_confined_launch

    assert_confined_launch(full, out, role="heldout_scoring", allow_open_fs=True)
    return full, out, private


def private_env(private: Path, *, base: dict[str, str] | None = None) -> dict[str, str]:
    """HOME, TMPDIR and CODEX_HOME inside the private directory, plus the network guard."""
    env = dict(base if base is not None else os.environ)
    for sub in ("home", "tmp", "codex_home"):
        (private / sub).mkdir(parents=True, exist_ok=True)
    # nothing of the repository on the confined PATH (uv run puts AdaMAS's own .venv/bin first)
    env["PATH"] = os.pathsep.join(p for p in (env.get("PATH") or "/usr/bin:/bin").split(os.pathsep) if p and not p.startswith(str(ROOT)))
    env.update({"HOME": str(private / "home"), "TMPDIR": str(private / "tmp"), "TMP": str(private / "tmp"),
                "TEMP": str(private / "tmp"), "CODEX_HOME": str(private / "codex_home"),
                "LD_PRELOAD": str(ensure_netguard()), "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "XDG_CACHE_HOME": str(private / "tmp" / "cache"), "PYTHONDONTWRITEBYTECODE": "1"})
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy", "UV_CACHE_DIR"):
        env.pop(k, None)
    return env


def confine_command(command: list[str], *, cwd: str | Path, env: dict[str, str] | None,
                    extra_write: list[str] | None = None) -> tuple[list[str], dict[str, str], Path | None]:
    """Wrap a driver-side run of workspace code (gate, repair evidence, resample frames) when enforced.

    Reads: system trees, the interpreter's venv, every existing path named on the
    command line or in PYTHONPATH (the harness script, suites, evidence copies);
    writes: ``cwd`` (the workspace), a private tmp and ``extra_write`` (the driver's own result-file directories,
    never a workspace of another run). Network as for agents.
    Returns (command, env, private dir to remove afterwards or None).
    """
    base_env = dict(os.environ if env is None else env)
    if not enforced() or not command:
        return command, base_env, None
    import tempfile

    command = _pin_pytest_root(list(command))
    private = Path(tempfile.mkdtemp(prefix="adamas-run-"))
    py = Path(command[0])
    # a venv is a directory with pyvenv.cfg; /usr/bin/python3 is not one (taking /usr as its "venv" granted all
    # of /usr, system dist-packages included -- caught by check_explicit_deny on 2026-10-11)
    venv = py.parent.parent if py.parent.name == "bin" and (py.parent.parent / "pyvenv.cfg").is_file() else None
    read = system_reads() + venv_reads(venv) + [str(ensure_netguard().parent)]
    if venv is None and py.is_absolute():
        read.append(str(py.parent))
    cands = [a.split("::", 1)[0] for a in command[1:]] + (base_env.get("PYTHONPATH") or "").split(os.pathsep)
    for a in cands:
        if a and os.path.isabs(a) and os.path.exists(a):
            for x in {a, os.path.realpath(a)}:          # a symlinked suite is checked at its real path
                read.append(x if os.path.isdir(x) else str(Path(x).parent))
    read += ancestor_configs(cwd)
    write = [str(cwd), *git_dirs(Path(cwd)), str(private), *DEV_WRITE, *[str(w) for w in (extra_write or [])]]
    # the gate's check script keeps its state beside itself in the run's harness directory
    # (package shadow, custody of the authored suite, the vacuous baseline repository)
    write += [str(Path(a).parent) for a in command[1:] if a.endswith("adamas_cpe_check.py") and os.path.isfile(a)]
    pol = Policy(read=read, write=write, connect_ports=net_ports(),
                 list_dirs=list_only(venv))
    pol_path = write_policy(pol, private / "policy.json")
    env = launcher_env(private_env(private, base=base_env))
    env["ADAMAS_SANDBOXED"] = "1"
    full = [*launcher(pol_path), *command]
    from orchestra.sandbox.guard import assert_confined_launch

    assert_confined_launch(full, env, role="workspace_code")
    return full, env, private


def _pin_pytest_root(command: list[str]) -> list[str]:
    """A pytest run whose targets sit outside its cwd roots itself at their common ancestor and lists it, which the
    sandbox refuses ("ERROR collecting ."). Pin ``--rootdir``/``--confcutdir`` to the targets' own common directory."""
    if "pytest" not in command[1:3] or any(a.startswith("--rootdir") for a in command):
        return command
    dirs = []
    for a in command[3:]:
        p = a.split("::", 1)[0]
        if os.path.isabs(p) and os.path.exists(p):
            dirs.append(p if os.path.isdir(p) else os.path.dirname(p))
    if not dirs:
        return command
    root = os.path.commonpath(dirs)
    i = command.index("pytest") + 1
    return [*command[:i], f"--rootdir={root}", f"--confcutdir={root}", *command[i:]]


def write_policy(policy: Policy, path: Path) -> Path:
    check_explicit_deny(policy)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy.to_dict()), encoding="utf-8")
    return path


def launcher(policy_path: Path) -> list[str]:
    """The prefix that confines and then execs: ``<python> -m orchestra.sandbox.exec --policy <p> --``."""
    return [sys.executable, "-m", "orchestra.sandbox.exec", "--policy", str(policy_path), "--"]


def launcher_env(env: dict[str, str]) -> dict[str, str]:
    """The launcher imports orchestra.sandbox before it confines itself."""
    out = dict(env)
    out["ADAMAS_SANDBOX_ORIG_PYTHONPATH"] = env.get("PYTHONPATH", "")
    out["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), *[p for p in (env.get("PYTHONPATH") or "").split(os.pathsep) if p]])
    return out


# --------------------------------------------------------------------------- manifest and self-checks


SELF_CHECK = r'''
import os, socket, sys, json
res = {}
def can_read(p):
    try:
        if os.path.isdir(p):
            os.listdir(p)
        else:
            open(p, "rb").read(1)
        return True
    except OSError:
        return False
for name, p in json.loads(sys.argv[1]).items():
    res["read:" + name] = "ALLOWED" if can_read(p) else "denied"
def conn(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect((host, port))
        return "connected"
    except OSError as e:
        return type(e).__name__
    finally:
        s.close()
res["net:external_443"] = conn("151.101.0.223", 443)
res["net:proxy"] = conn("127.0.0.1", int(sys.argv[2]))
print(json.dumps(res))
'''


def self_check(policy: Policy, env: dict[str, str], probes: dict[str, str], *, python: str = "/usr/bin/python3") -> dict[str, Any]:
    """Run probes inside the exact launcher an agent gets; returns their outcome."""
    d = Path(env.get("TMPDIR") or "/tmp")
    pol = write_policy(policy, d.parent / "selfcheck_policy.json")
    proc = subprocess.run([*launcher(pol), python, "-c", SELF_CHECK, json.dumps(probes), str(proxy_port())],
                          capture_output=True, text=True, env=launcher_env(env), timeout=60)
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = {"error": proc.stderr[-500:]}
    ok = all(v == "denied" for k, v in out.items() if k.startswith("read:")) and out.get("net:external_443") != "connected" \
        and out.get("net:proxy") == "connected"
    return {"results": out, "ok": ok}


def default_probes() -> dict[str, str]:
    """Paths an agent must not read: memory/, dataset (held-out + reference), outputs/, configs/, the source, ~/.codex, /tmp."""
    ds = Path("/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset")
    adamas = Path("/root/projects")
    return {"memory": str(adamas / "AdaMAS" / "memory" / "VERSION"),
            "adamas_source": str(adamas / "AdaMAS" / "src"), "adamas_outputs": str(adamas / "AdaMAS" / "outputs"),
            "adamas_single_outputs": str(adamas / "AdaMAS-single" / "outputs"), "dataset": str(ds), "heldout": str(ds / "tinydb" / "unit_tests"),
            "reference": str(ds / "tinydb" / "tinydb"), "outputs": str(ROOT / "outputs"), "configs": str(ROOT / "configs"),
            "source": str(ROOT / "src"), "codex_home": "/root/.codex", "global_tmp": "/tmp"}


def write_manifest(run_dir: Path, entry: dict[str, Any]) -> None:
    p = Path(run_dir) / "sandbox_manifest.json"
    data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {
        "scheme": "landlock (fs + tcp connect ports) + LD_PRELOAD netguard (loopback only) + turn-item audit",
        "landlock_abi": abi_version(), "netguard": str(ensure_netguard()), "network": {
            "tcp_connect_ports": [proxy_port(), f"{EPHEMERAL[0]}-{EPHEMERAL[1]}"],
            "dynamic_programs": "connect/sendto/sendmsg only to loopback or AF_UNIX",
            "static_programs": "tcp connect only to the ports above", "udp": "loopback only for dynamic programs; not covered by landlock"},
        "entries": []}
    data["entries"].append(entry)
    p.write_text(json.dumps(data, indent=1), encoding="utf-8")


def clean_private(private: Path) -> None:
    shutil.rmtree(private, ignore_errors=True)


__all__ = [
    "CONFIG_PATH", "ENFORCED_ENV", "EXPLICIT_DENY", "SEALED_ARCHIVE_DIR", "SandboxRequiredError", "check_explicit_deny",
    "explicit_deny_paths", "TEST_UNCONFINED_ENV", "VENV_ENV", "agent_policy", "confine_command", "confine_scoring", "default_probes", "enforced", "ensure_netguard", "gate_policy",
    "git_dirs", "launcher", "launcher_env", "load_config", "net_ports", "private_env", "proxy_port", "scoring_policy",
    "self_check", "write_manifest", "write_policy",
]
