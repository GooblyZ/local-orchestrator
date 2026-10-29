"""The real Codex / Claude Code CLIs as the browser app uses them: the installed-model catalog and the agent factory.
Nothing here contacts a model: the catalog and the preflight use non-inference CLI commands only."""
from __future__ import annotations

from config import HERE

# The newest model of each Claude family, pinned by full name (an alias like "opus" would follow whatever the CLI maps
# it to). Offered only when the installed `claude --help` lists that family for --model.
LATEST_CLAUDE = {"fable": ("claude-fable-5-1", "Fable 5.1"), "opus": ("claude-opus-5-5", "Opus 5.5"),
                 "sonnet": ("claude-sonnet-5-5", "Sonnet 5.5")}


def default_catalog(cfg) -> dict:
    """What the INSTALLED CLIs offer, read without inference: Codex's model catalog (selectable models + their effort
    levels, never 'ultra' or a speed tier) and the --model aliases / --effort levels listed by `claude --help`."""
    from claude_adapter import ClaudeCli, help_efforts, help_model_aliases
    from codex_adapter import CodexCli
    helptext = ClaudeCli(cfg)._run(["--help"], str(HERE), max_output=1_000_000).stdout
    return {"codex": CodexCli(cfg).selectable_models(),
            "claude": {"models": [{"id": LATEST_CLAUDE[a][0], "name": LATEST_CLAUDE[a][1]}
                                  for a in help_model_aliases(helptext) if a in LATEST_CLAUDE],
                       "efforts": help_efforts(helptext)}}


def default_agents(cfg, ws):
    """The real CLIs (subscription logins). Returns (planner, executor, reviewer); preflight is non-inference."""
    from claude_adapter import ClaudeCli, ClaudeExecutor
    from codex_adapter import CodexCli, CodexPlanner, CodexReviewer
    codex, claude = CodexCli(cfg), ClaudeCli(cfg)
    codex.preflight()
    claude.preflight(str(ws))
    return CodexPlanner(codex), ClaudeExecutor(claude), CodexReviewer(codex)
