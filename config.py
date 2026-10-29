"""Configuration loading/validation and workspace validation."""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path

from store import read_json

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = {
    "max_cycles": 8,
    "quiet_notice_sec": 600,  # goal mode: no observable agent activity for this long -> tell the owner (never kills the call)
    "review_material_chars": 150000,  # research review: report + research files per Codex call; more = reviewed part by part
    "max_executor_turns": 20,  # Claude's internal tool turns per call; checked after the call (the CLI has no --max-turns)
    "process_timeout_sec": 120,
    "verification_timeout_sec": 300,
    "max_no_progress": 2,
    "min_tests_ran": 1,
    "require_changes": True,
    "max_instruction_chars": 8000,
    "max_codex_calls": 0,   # per-run model-call budgets; 0 = derived from max_cycles (run_policy.budget_limits)
    "max_claude_calls": 0,
    "runs_dir": "runs",
    "verify_command": ["python", "-m", "unittest", "discover", "-s", "tests"],
    "command_allowlist": [["python", "-m", "unittest"], ["python", "-m", "pytest"]],
    "protected_path_patterns": [".env", ".env.*", "*.pem", "*.key", "id_rsa*", "auth.json",
                                "credentials*", ".git", ".ssh", ".claude", ".codex", ".aws"],
    "snapshot_ignore_dirs": [".git", "__pycache__", "node_modules", ".venv", "venv"],
    "max_file_write_bytes": 1000000,
    "deletes_require_approval": True,
    "live": {"enabled": False, "approval_file": "live_approval.json"},
    "codex": {
        "executable": "toolchain/codex/node_modules/.bin/codex.cmd",
        "home": "~/.codex-orchestrator-home",
        "pinned_version": "0.162.1",
        "model": "gpt-6-sol",
        "reasoning_effort": "high",
        "web_search": True,  # Codex's native web_search tool (`codex --search`); still read-only, no shell, no file changes
        "timeout_sec": 180,
        "max_prompt_chars": 400000,
        "required_disabled_features": ["hooks", "plugins", "apps", "shell_tool", "skill_search", "fast_mode",
                                       "ultrafast_mode", "multi_agent", "memories", "plugin_sharing",
                                       "remote_plugin"],
    },
    "viewer": {"web": True, "auto_open_browser": True, "port": 0},
    "claude": {
        "executable": "claude.cmd",
        "pinned_version": "2.1.293",
        "model": None,   # None = Claude Code's default model; or an alias / full name accepted by `claude --model`
        "effort": None,  # None = Claude Code's default; else one of the levels listed by `claude --help` for --effort
        "permission_mode": "dontAsk",
        "timeout_sec": 180,
        "max_prompt_chars": 400000,
        "tools": ["Read", "Glob", "Grep", "Edit", "Write", "Bash", "WebSearch", "WebFetch"],
        "allowed_tools": ["Read", "Glob", "Grep", "Edit", "Write", "Bash(python -m unittest *)", "Bash(python -m pytest *)",
                          "WebSearch", "WebFetch"],
        # credential files are unreadable (so they can never reach the web); no destructive, download or install commands
        "disallowed_tools": ["Bash(git push *)", "Bash(rm *)", "Bash(del *)", "Bash(curl *)", "Bash(wget *)", "Bash(pip *)",
                             "Read(**/.env)", "Read(**/.env.*)", "Read(**/*.pem)", "Read(**/*.key)", "Read(**/id_rsa*)", "Read(**/auth.json)", "Read(**/credentials*)"],
    },
}


class ConfigError(Exception):
    pass


class OrchestratorError(Exception):
    pass


def load_config(path=None, overrides=None) -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path:
        user = read_json(path)
        unknown = set(user) - set(DEFAULT_CONFIG)
        if unknown:
            raise ConfigError(f"unknown config keys: {sorted(unknown)}")
        cfg.update(user)
    cfg.update(overrides or {})
    ints = {"max_cycles": (1, 50), "max_executor_turns": (1, 50), "quiet_notice_sec": (1, 86400), "review_material_chars": (20000, 300000), "process_timeout_sec": (1, 3600),
            "verification_timeout_sec": (1, 3600), "max_no_progress": (1, 10),
            "min_tests_ran": (0, 10000), "max_instruction_chars": (500, 50000),
            "max_file_write_bytes": (1, 50_000_000), "max_codex_calls": (0, 200), "max_claude_calls": (0, 200)}
    for k, (lo, hi) in ints.items():
        v = cfg[k]
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise ConfigError(f"{k} must be an integer in [{lo}, {hi}]")
    for k in ("require_changes", "deletes_require_approval"):
        if not isinstance(cfg[k], bool):
            raise ConfigError(f"{k} must be boolean")
    ok_list = lambda x: isinstance(x, list) and x and all(isinstance(i, str) and i for i in x)  # noqa: E731
    if not ok_list(cfg["verify_command"]):
        raise ConfigError("verify_command must be a non-empty list of strings")
    if not (isinstance(cfg["command_allowlist"], list) and all(ok_list(e) for e in cfg["command_allowlist"])):
        raise ConfigError("command_allowlist must be a list of string lists")
    for k in ("protected_path_patterns", "snapshot_ignore_dirs"):
        if not isinstance(cfg[k], list) or not all(isinstance(i, str) for i in cfg[k]):
            raise ConfigError(f"{k} must be a list of strings")
    live = cfg["live"]
    if (not isinstance(live, dict) or set(live) - {"enabled", "approval_file"}
            or not isinstance(live.get("enabled"), bool) or not isinstance(live.get("approval_file", ""), str)):
        raise ConfigError("live must be {enabled: bool, approval_file: str}")
    vw = cfg["viewer"]
    if (not isinstance(vw, dict) or set(vw) != {"web", "auto_open_browser", "port"} or not isinstance(vw["web"], bool)
            or not isinstance(vw["auto_open_browser"], bool) or not isinstance(vw["port"], int)
            or isinstance(vw["port"], bool) or not 0 <= vw["port"] <= 65535):
        raise ConfigError("viewer must be {web: bool, auto_open_browser: bool, port: 0..65535} (always bound to 127.0.0.1)")
    cl = cfg["claude"]
    if not isinstance(cl, dict) or set(cl) != set(DEFAULT_CONFIG["claude"]):
        raise ConfigError(f"claude section must have exactly these keys: {sorted(DEFAULT_CONFIG['claude'])}")
    for k in ("executable", "pinned_version"):
        if not isinstance(cl[k], str) or not cl[k].strip():
            raise ConfigError(f"claude.{k} must be a non-empty string")
    if cl["model"] is not None and (not isinstance(cl["model"], str) or not cl["model"].strip()):
        raise ConfigError("claude.model must be null (CLI default) or a non-empty string")
    if cl["effort"] is not None and cl["effort"] not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigError("claude.effort must be null or one of low, medium, high, xhigh, max")
    if cl["model"] is not None and not re.fullmatch(r"[A-Za-z0-9._\[\]-]{1,80}", cl["model"]):
        raise ConfigError("claude.model must be an alias or model name (letters, digits, . _ - [ ])")
    if cl["permission_mode"] not in ("dontAsk", "default", "plan"):
        raise ConfigError("claude.permission_mode must be dontAsk, default or plan (never acceptEdits/auto/bypass)")
    for k, lo, hi in (("timeout_sec", 5, 3600), ("max_prompt_chars", 1000, 500000)):
        if not isinstance(cl[k], int) or isinstance(cl[k], bool) or not lo <= cl[k] <= hi:
            raise ConfigError(f"claude.{k} must be an integer in [{lo}, {hi}]")
    for k in ("tools", "allowed_tools", "disallowed_tools"):
        v = cl[k]
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            raise ConfigError(f"claude.{k} must be a list of non-empty strings")
        if any(re.search(r'[%^&|<>"`,]', x) for x in v):
            raise ConfigError(f"claude.{k} contains shell-significant characters")
    broad = [x for x in cl["allowed_tools"] if x.strip() in ("*", "Bash", "Bash(*)", "Bash(**)")
             or x.startswith("Bash(*") or x.startswith("mcp__")]
    if broad:
        raise ConfigError(f"claude.allowed_tools has overly broad entries: {broad}")
    if (cl["tools"] and not cl["allowed_tools"]) or set(cl["allowed_tools"]) & set(cl["disallowed_tools"]):
        raise ConfigError("claude.allowed_tools must be non-empty (unless tools is [] = no tools) and disjoint from disallowed_tools")
    if not cl["tools"] and cl["allowed_tools"]:
        raise ConfigError("claude.tools is [] (no tools): allowed_tools must be [] too")
    cx = cfg["codex"]
    if not isinstance(cx, dict) or set(cx) != set(DEFAULT_CONFIG["codex"]):
        raise ConfigError(f"codex section must have exactly these keys: {sorted(DEFAULT_CONFIG['codex'])}")
    for k in ("executable", "home", "pinned_version", "model"):
        if not isinstance(cx[k], str) or not cx[k].strip():
            raise ConfigError(f"codex.{k} must be a non-empty string")
    if not isinstance(cx["web_search"], bool):
        raise ConfigError("codex.web_search must be true or false")
    if cx["reasoning_effort"] not in ("low", "medium", "high", "xhigh", "max"):  # "ultra" (multi-agent delegation) is never allowed
        raise ConfigError("codex.reasoning_effort must be low, medium, high, xhigh or max")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", cx["model"]):
        raise ConfigError("codex.model must be a model slug")
    for k, lo, hi in (("timeout_sec", 5, 600), ("max_prompt_chars", 1000, 500000)):
        if not isinstance(cx[k], int) or isinstance(cx[k], bool) or not lo <= cx[k] <= hi:
            raise ConfigError(f"codex.{k} must be an integer in [{lo}, {hi}]")
    if (not isinstance(cx["required_disabled_features"], list) or not cx["required_disabled_features"]
            or not all(isinstance(x, str) for x in cx["required_disabled_features"])):
        raise ConfigError("codex.required_disabled_features must be a non-empty list of strings")
    if not isinstance(cfg["runs_dir"], str) or not cfg["runs_dir"]:
        raise ConfigError("runs_dir must be a non-empty string")
    return cfg


def resolve_runs_dir(cfg) -> Path:
    p = Path(cfg["runs_dir"])
    return p if p.is_absolute() else HERE / p


def validate_workspace(ws, runs_dir) -> Path:
    p = Path(os.path.realpath(ws))
    if not p.is_dir():
        raise OrchestratorError(f"workspace is not a directory: {ws}")
    rd = Path(os.path.realpath(runs_dir))
    if p.parent == p or p == Path.home().resolve():
        raise OrchestratorError("refusing to use a filesystem root or the home directory as workspace")
    if HERE == p or HERE.is_relative_to(p) or rd == p or rd.is_relative_to(p) or p.is_relative_to(rd):
        raise OrchestratorError("workspace must not contain, or be inside, the orchestrator/runs directories")
    return p


def validate_project_root(path, runs_dir, cfg, max_files: int = 20000) -> Path:
    """An EXISTING project folder the owner authorizes as the agents' working root. Stricter than validate_workspace:
    an absolute path the owner typed, never a folder inside the orchestrator (its old isolated workspace copies included),
    never an ancestor of the home folder, never inside a protected folder (.git, .claude, .ssh, ...), and small enough to
    snapshot. Returns the resolved path; nothing is created, changed or copied here."""
    raw = str(path or "").strip().strip('"')
    if not raw or "\x00" in raw:
        raise OrchestratorError("no folder given")
    if not os.path.isabs(raw) or (os.name == "nt" and not re.match(r"^[A-Za-z]:[\\/]", raw)):
        raise OrchestratorError("the folder must be a full absolute path (e.g. C:\\Users\\me\\projects\\my-app)")
    p = validate_workspace(raw, runs_dir)
    if p.is_relative_to(HERE):
        raise OrchestratorError("this folder is inside the orchestrator (for example an old isolated workspace copy); "
                                "choose the real project folder")
    if Path.home().resolve().is_relative_to(p):
        raise OrchestratorError("refusing a folder that contains the home directory")
    import fnmatch
    protected = [x.lower() for x in cfg["protected_path_patterns"]]
    for part in p.parts[1:]:
        if any(fnmatch.fnmatch(part.lower(), pat) for pat in protected):
            raise OrchestratorError(f"refusing a folder inside a protected location ({part})")
    ignore, n = set(cfg["snapshot_ignore_dirs"]), 0
    for _dirpath, dirnames, filenames in os.walk(p):
        dirnames[:] = [d for d in dirnames if d not in ignore]
        n += len(filenames)
        if n > max_files:
            raise OrchestratorError(f"the folder has more than {max_files:,} files; too large to track changes safely")
    return p
