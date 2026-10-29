# Local AI Orchestrator — Codex manages, Claude Code programs, Python relays

**Use it:** double-click `start.cmd`. The browser app opens on 127.0.0.1, with a home screen and the full flow:
1. Type the task and press "התחל משימה".
2. Check the confirmation and accept local execution.
3. Watch Codex and Claude work.
4. Approve or deny permissions.
5. Pause, cancel or resume.
6. See the result and the files.

No terminal input is needed; the console window only keeps the local server running. A second `start.cmd` reuses the running app. The terminal launcher `python orchestrator.py start` still works. Hebrew guide: [docs/user-guide-he.md](docs/user-guide-he.md).

**Browser app security.** The read-only `serve` viewer stays GET-only. In app mode (`orchestrator.py app`, `app_service.py`) every state change is a POST that needs:
- the per-launch token in a header (never the URL);
- a CSRF value that exists only inside the served page;
- an Origin of `http://127.0.0.1:<port>`;
- a JSON body of at most 2,000,000 bytes (`web_viewer.MAX_POST_BYTES`).

**Long tasks.** The task field accepts up to 200,000 Unicode characters (`app_service.MAX_TASK_CHARS`; larger tasks get `task_too_long` with the exact count). Prompts go to both CLIs on stdin, never on the command line, and are never truncated (`max_prompt_chars` 400,000). Above 4,000 characters the full task is also saved in `<workspace>/.orchestrator/TASK.md` (hash-checked and restored each cycle); Claude gets the whole text in its first message and a reference to the file on resumed turns. Codex calls are ephemeral and cannot read files, so the reviewer receives the COMPLETE task on stdin at every review (never clipped; only the evidence part is shortened if the prompt would exceed `max_prompt_chars`). From the terminal: `start --task-file spec.md`.

**Long model calls (goal mode).** There is no per-call wall-clock limit for Codex or Claude and no per-call turn limit: one call may legitimately research for an hour. What bounds a goal-mode task is the owner's overall budget (minutes, Codex calls, Claude calls), plus pause and stop. While a call runs, the orchestrator polls about every 0.2 s:
- the owner's **stop** kills the process tree immediately;
- the overall **time budget** ends the call and PAUSES the task (`budget_reached: time`). Files on disk and the Claude session are kept, and after the owner extends the budget the same session continues;
- **silence is never treated as failure**. After `quiet_notice_sec` (600) without observable activity, the owner is told that the process is still running and can keep waiting or stop it.

Claude's tool use (searches, fetches, edits, denials) is shown live by reading Claude Code's own session log, read-only, under `CLAUDE_CONFIG_DIR` or `~/.claude`. If that log cannot be found, the owner is told that activity is not observable. Real CLI errors are still classified (auth / quota → BLOCKED, process errors → FAILED). With `--permission-mode dontAsk` Claude never waits on a prompt; denials are reported at the end of the call. Fixed mode keeps the configured per-call `timeout_sec` and `max_executor_turns`.

**Research rubric.** A research task's acceptance criteria are derived from the task itself. The orchestrator enforces only a domain-neutral minimum:
- `RESEARCH_REPORT.md` exists;
- it has a sources section (`## Sources` or `## מקורות`) with retrieval dates;
- it ends with a `Verdict:` line in the task's own decision format (e.g. GO / PIVOT / NO-GO / INCONCLUSIVE).

Out-of-sample and after-cost evidence is required only for research about a trading edge.

**Research review.** Codex reviews the COMPLETE report and every text file of the research folder, never an excerpt. Files are split into traceable segments (path, line range, sha256). When the material exceeds `review_material_chars` (150,000), Codex reviews it part by part: one counted call per part, with its own notes carried into the final review call, and a coverage manifest accounting for every segment. Files that cannot be included (binary, too large) are listed, not dropped. The full texts go to Codex only; the transcript keeps the manifest.

Codex's own verdict is the independent review and approval. Claude is told never to write or simulate it, and the reviewer disregards approvals written on its behalf. For research, the reviewer may use read-only web search (`--search`) to verify key claims; its queries are recorded.

When a research run resumes after the reviewer asked the owner (or after a budget pause during a review), it continues with a review-only cycle: Codex reviews the existing work, Claude is not called, and the call counts against the Codex budget.

Other rules:
- A GET can never change anything.
- Preparing a task runs only the non-inference preflight.
- A run starts only via a separate one-time confirmation id plus `accept_local_execution: true`, so typed task text is never an approval.
- Only one task runs at a time.
- A permission approval must name exactly the rule Claude requested; blanket shell access is never approvable.

**Internet research (on by default)**

*Claude Code* has its native `WebSearch` and `WebFetch` tools, pre-approved: public information and public data, including market-data APIs over HTTPS. It must list every source as `url`, `retrieved_at` and `used_for` in its report's `sources`, and research reports need a dated Sources section.
- Credential files are unreadable: `Read(**/.env)`, `*.pem`, `*.key`, `id_rsa*`, `auth.json` and `credentials*` are denied, so they cannot reach the web.
- `curl`, `wget`, `pip`, `rm`, `del` and `git push` stay denied.
- A data-download script needs your per-command approval before it can run.

*Codex* uses its native `web_search` tool (`codex --search`) and stays read-only: any command or file-change item is refused. Its queries are recorded with their messages (`meta.web_searches`).

*Never allowed:* sign-ups, logins, purchases, trades, paid API keys or fallback models, or downloading and running code. The owner can switch Codex search off with `codex.web_search: false`.

**Goal-driven execution (default) vs. fixed cycles** (`goal_mode.py`)

*Goal mode.* Codex plans, Claude works, the orchestrator verifies and Codex reviews, repeated until the deterministic gate AND the reviewer confirm the goal. It is not an endless loop: every task has persisted budgets for running time, Codex calls, Claude calls and a cycle checkpoint.
- **Budget reached.** The task is PAUSED (`budget_reached:*`) with all state kept.
- **Extending a budget** is an owner-only action: browser "הגדל תקציב והמשך", or `orchestrator.py extend-budget --cycles N --minutes M --codex-calls X --claude-calls Y`. Budgets only go up, within hard caps.
- **Resume** never resets a counter or widens a budget.
- **Agents cannot change budgets.** Budget/limit/quota-like keys in agent JSON are ignored and logged.
- **Stalls stop and ask you** (HUMAN_REQUIRED `stalled:*`): the same instruction or the same verification failure 3 times, or no measurable progress in 2 cycles.
- **Quota/auth errors** stop as BLOCKED.

*Progress display.* Codex reports `criteria_status` per acceptance criterion. It is display-only; completion is decided by evidence.

*Fixed mode.* The previous capped behaviour (max cycles, then NEEDS_REVISION).

*Research tasks (kind `research`)*
- **What Claude delivers:** `RESEARCH_REPORT.md` with these sections: Hypotheses, Data and method, Historical evidence, Out-of-sample validation, Costs and net performance, Unresolved uncertainty, Conclusion. The report ends with `Verdict: SUPPORTED | NOT_SUPPORTED | INCONCLUSIVE`.
- **What the orchestrator checks:** structure and the presence of evidence, without executing anything. A SUPPORTED verdict without measured out-of-sample and after-cost figures is refused.
- **How it ends:** an INCONCLUSIVE verdict ends the task as `INCONCLUSIVE` (exit 8), never as success. Success means "report verified for structure and evidence presence, not for truth".
- **What Codex is told:** to judge the actual report and to prefer INCONCLUSIVE over overclaiming.
- **Hard limits:** no trading, spending or account access. Network tools remain denied.

**Agent settings (home screen → "הגדרות סוכנים").** The choices come from the installed CLIs, without inference:
- Codex models and effort levels come from `codex debug models`. Only listed models are offered; `ultra` (multi-agent delegation) and Fast tiers never are.
- Claude's `--model` aliases and `--effort` levels come from `claude --help`.
- Maximum cycles: 1-5.

Choices are validated server-side, saved to `runs/_app_prefs.json`, recorded in the run state (`agent_settings`, also used on resume) and shown in the conversation side panel. A model that the subscription does not actually serve is only discovered at the first call, which then stops as `BLOCKED`.

The flow is `User → Codex plans → Claude Code works → the orchestrator re-runs the tests → Codex reviews the real changes and test output → corrections → stop`.

There are exactly two AI agents:
- **Codex** (gpt-6-sol, ChatGPT subscription, read-only, no tools): plans, instructs, reviews and decides.
- **Claude Code** (Claude Max subscription, the normal `claude` CLI): does the coding.

The Python orchestrator is not an agent. It relays messages, keeps the Claude session, enforces cycles, call budgets and timeouts, handles cancellation and your approvals, preserves the transcript and shows it live in a Hebrew browser viewer.

**Profiles**
- **NATIVE (default).** Claude Code works as usual inside the task folder: Read, Glob, Grep, Edit, Write, and Bash for the project's tests.
  - It runs with `--permission-mode dontAsk` and `claude.allowed_tools`, so anything not pre-approved is *denied*, never silently granted.
  - When Claude needs more (say `Bash(npm test)`), the run pauses and shows the exact request. You approve or deny it **for this task only**, and the same Claude session continues.
  - You accept local execution by typing "כן" when starting. The code runs on this PC under your Windows permissions, which carries the same general risks as using Claude Code normally. No Docker, Windows Pro or sandbox is required.
  - Codex reviews the real changed file contents and the orchestrator's own test run, not just Claude's report.

**Existing project mode** (browser: *עבודה על פרויקט קיים*). The owner types the absolute path of a real project and ticks the authorization box; `config.validate_project_root` refuses relative paths, the home folder or its ancestors, anything inside the orchestrator (including its old `workspaces/` copies) or the runs folder, folders inside protected locations (`.git`, `.claude`, `.ssh`, ...) and folders with more than 20,000 files. An existing-project request without a usable folder is refused (`project_folder_required`); it never falls back to a new or old workspace. The folder is validated again when the owner confirms, and again before every resume. The state records `project = {mode: existing_project, root, authorized_at, handoff}` and the task page shows the root on top. Claude's working directory is the root (Claude Code `--restricted`: file tools confined to it; `--add-dir` stays forbidden). Codex keeps its empty read-only temp folder and no tools; the planner gets the root's relative file list and the reviewer the real changes and test run, all read by the orchestrator (no local paths). A long task file is written as `.orchestrator/TASK-<task_id>.md` with exclusive create, so no project file is overwritten. Nothing is ever copied or synchronized between folders.
**Delegated autonomy** (`autonomy.py`, `autopilot.py`, `capgate.py`; browser: *כמה עצמאות לתת לסוכנים*). The owner picks a level once per task (`supervised`, `balanced` (default), `autonomous`; changeable later via `/api/app/autonomy`); tasks created earlier keep their old behaviour (`legacy`). A deterministic policy, never model text, decides:
- *Reviews*: Codex returns a structured `outcome` (approved / material_corrections / minor_only / insufficient_evidence / owner_decision_required / failed) plus `owner_decision` (category, action, purpose, affected, risks, alternatives). A HUMAN_REQUIRED about a technical matter is sent back to Codex once to decide (counted call); owner-level categories (money, credentials, scope, destructive data, publication/external effects, system changes, security controls) or matching words always reach the owner with a decision card. Material corrections go to Claude automatically; minor-only reviews do not block a verified result. COMPLETE also requires every reported criterion `met`; twice-unverifiable acceptance and polishing loops after green tests stop for the owner.
- *Capability gateway*: Claude gets one extra allow rule, `Bash(python -I <orchestrator>/capgate.py *)`. `test` (unittest/pytest with targets, repeat, tail, grep, exit code; skipped tests reported as not-evidence), `script` (project `.py` files: screened for network/process/deletion/system/AI-CLI use; clean ones run automatically at balanced/autonomous, others, and all at supervised, need the owner once per exact file hash) and `env` (read-only diagnostics). Explicit argv, no shell, project root as cwd, timeout, audit log in `runs/<task>/capabilities.jsonl`; the per-task policy file lives outside the project. Blocked routine shell compositions (cd/&&/pipes/loops around tests or project scripts) are redirected to the gateway in the same Claude session; anything else stays an owner decision. Approved scripts persist in the task (`approved_scripts`).
- *Recovery*: failures are classified (`autonomy.classify_failure`). Transient errors are retried with backoff within budget; an unavailable model falls back only to a model the owner chose at task creation and the CLI offers; configuration/auth/quota errors stop at once with a concrete diagnosis (the orchestrator never edits CLI configuration); a technically failed review is retried alone (code tasks too), never re-running Claude.
- *Transparency*: `state.decisions` (every autonomous decision with its reason), `state.metrics` (owner interventions, recoveries, gateway runs, calls/time per role), shown in the task page panel *החלטות שהתקבלו לבד*.
- *Saved and legacy tasks*: a task created before autonomy levels has `autonomy: null` and behaves as supervised ("legacy"); its waiting card explains this and offers *איך להמשיך מכאן* (level, AI calls by project scripts, Codex fallback), saved through `/api/app/autonomy` (validated against the installed CLIs, logged) and then resumed - same task, history, binding, budget, approvals and files. Resuming a Codex question under balanced/autonomous first runs a review-only cycle in which Codex re-decides under the new policy (Claude does not repeat its last step).
- *Models and the product's own AI features*: Codex receives the authorized models and the installed CLI's catalog (`codex debug models`, no inference) with every review, so it never needs to ask the owner to look at `/model`. Project scripts that start the Claude/Codex CLIs run through the gateway only within the owner's `script_ai_runs` allowance and only with an explicitly passed authorized `--model`; beyond it, the owner approves the exact script version. If the orchestrator's own model is unavailable and no authorized fallback works, the task pauses (`model_authorization_required`) with the offered models; the owner picks one (`use_model`) and resumes.
- *Typed agent operations* (no project script needed): Claude adds `agent_requests: [{op: "codex_review", artifact, focus, id}]` to its report; the orchestrator itself runs each one with the task's authorized Codex model through the same isolated, read-only adapter (`CodexReviewer.review_artifact`), checks scope (a readable, non-protected project file), the owner's `agent_reviews` allowance and the Codex budget, retries a malformed review once (review only), records the result (`state.agent_ops`, `runs/<task>/agent_ops.json`, readable through `capgate results`), gives it to Codex's cycle review and, automatically, to Claude at its next step.
- *Mission-start capability contract*: the setup form authorizes, once, artifact reviews, the product's own AI-integration scripts (declared by path) and how many real AI CLI calls they may make. The planner predicts `capabilities_needed`; anything not yet authorized is presented together, once, right after planning and before any work (`mission_capabilities_required`). Declared scripts run through the gateway behind `aishim.py`, which sits first on PATH as `codex`/`claude` and counts every real inference call, enforces authorized models and refuses bypass flags, in code. Screening is AST-based and follows project imports transitively; network, system access, dangerous launched commands (git, curl, powershell...) or unparseable code still stop for the owner, declared or not. Approving a script that starts the AI CLIs declares it (metered) instead of approving one file version.
- *Not an OS sandbox*: tests and screened scripts still run as normal processes under the owner's account (as `python -m unittest` always did); screening is heuristic. See the owner report for the remaining isolation options.

*Handoff*: a saved task is never re-pointed at another folder (its Claude session, baseline snapshot, lock and evidence belong to its old folder). Instead, a new existing-project task can take the saved task's context (`handoff.py`: original task, plan, criteria, latest summary, recent history, Codex's open question; no old path). When the new task starts, the source only gains a `handed_off_to` marker plus an event and transcript notice; resume, continue and budget actions on it are then refused (`handed_off`), from the browser and the command line.
- **RESTRICTED (optional, `start --restricted --allow FILE`).** Claude has no tools and only proposes files. The orchestrator writes allowlisted files transactionally and checks them statically; nothing generated is executed.

**What was verified**
- **Restricted profile:** one live run (`runs/20261012-193015-mvp-live-acceptance`).
- **Native profile:** the native integration (permissions, approvals, session continuation, Codex evidence) is covered by simulated-CLI tests. No live native run has been made yet.

## Start the local server (browser app)

In this section `<project>` means the folder you cloned this repository into, for example
`C:\Users\User\VibeCoding\projects\local-ai-orchestrator`. Every path below is written out in full.

**Requirements (once):**
1. **Python 3.11+** on PATH. Check in any terminal: `python --version`. The orchestrator itself uses only the standard library.
2. **Claude Code CLI** installed globally and signed in with your Claude subscription. Check in any terminal:
   `where claude` (must find `claude.cmd`) and `claude auth status` (must show `claude.ai`).
   The expected version is `claude.pinned_version` in `<project>\config.json`.
3. **Node.js + npm** on PATH (`npm --version`), then the **project-local Codex CLI**. In a terminal opened in `<project>\toolchain\codex`:
   ```
   cd <project>\toolchain\codex
   npm install @openai/codex@0.162.1
   ```
   This creates `<project>\toolchain\codex\node_modules\.bin\codex.cmd` - the only Codex the orchestrator runs
   (the global Codex install is never used).
4. **Codex's own home folder** `%USERPROFILE%\.codex-orchestrator-home` (e.g. `C:\Users\<you>\.codex-orchestrator-home`),
   separate from your normal `%USERPROFILE%\.codex`. Create it and sign in there with your ChatGPT account, in a
   **cmd** terminal opened in `<project>`:
   ```
   cd <project>
   mkdir "%USERPROFILE%\.codex-orchestrator-home"
   set CODEX_HOME=%USERPROFILE%\.codex-orchestrator-home
   toolchain\codex\node_modules\.bin\codex.cmd login
   ```
   Its settings file is `%USERPROFILE%\.codex-orchestrator-home\config.toml`; it must contain `forced_login_method = "chatgpt"`,
   `sandbox_mode = "read-only"`, `approval_policy = "never"`, `[skills] include_instructions = false`, no MCP servers and no
   `service_tier`. See [Codex adapter](#codex-adapter-planner--reviewer) for every required setting.
5. Check both CLIs without any model call, in a terminal opened in `<project>` - each prints exactly what is still missing:
   ```
   cd <project>
   python orchestrator.py codex-preflight
   python orchestrator.py claude-preflight
   ```

**Start it:**
- **Double-click `<project>\start.cmd`**, or in a terminal opened in `<project>` run:
  ```
  cd <project>
  python -B orchestrator.py app
  ```
- The console prints the address, e.g. `http://127.0.0.1:55295/?t=<token>`, and your browser opens it automatically. The server
  listens on **127.0.0.1 only** (this computer); the `t=` token in the URL is required, so use the printed link.
- **Keep the console window open** while you use the app or a task runs (you may minimize it). Closing it stops the server; a
  running task then shows as interrupted and can be resumed later - nothing is lost.

**Options** (in a terminal opened in `<project>`; they go before `app`):
```
cd <project>
python -B orchestrator.py --port 8765 app     # a fixed port instead of a random free one -> http://127.0.0.1:8765/?t=<token>
python -B orchestrator.py --no-open app       # do not open the browser; just print the URL
```

**Where things are:**
| What | Path |
|------|------|
| Start file | `<project>\start.cmd` |
| Settings (models, timeouts, permissions) | `<project>\config.json` |
| Address of the running app (`url`) | `<project>\runs\_app.json` |
| Every task's record (state, conversation, events) | `<project>\runs\<task_id>\` |
| New isolated task folders | `<project>\workspaces\<date>-<name>\` |
| Codex CLI used by the orchestrator | `<project>\toolchain\codex\node_modules\.bin\codex.cmd` |
| Codex home (login + `config.toml`) | `%USERPROFILE%\.codex-orchestrator-home\` |
| Hebrew user guide | `<project>\docs\user-guide-he.md` |

**Good to know:**
- Starting it again while it runs (a second `<project>\start.cmd`) does not start a second server: it reopens the running app.
- To stop: close the console window, or press `Ctrl+C` in it.
- If the page says a preflight failed, run the two preflight commands from step 5; nothing calls a model until you confirm a
  task in the browser.

## Setup
Python 3.11+ (uses `tomllib`) and the standard library only. Nothing to install for the orchestrator itself.
The pinned Codex CLI lives in `toolchain/codex` (project-local, git-ignored); the global Codex install is never used.

```
cd C:\Users\User\VibeCoding\projects\local-ai-orchestrator
python orchestrator.py preflight          # shows which env var NAMES are stripped from child processes
python orchestrator.py demo               # end-to-end mock run in a temporary workspace
python orchestrator.py claude-preflight   # NON-inference checks of the real Claude CLI (version, subscription login, flags); lists what is NOT enforceable
python orchestrator.py demo --claude-sim  # real Claude ADAPTER + SIMULATED Claude CLI (see docs/sample_claude_adapter_conversation.txt)
python orchestrator.py codex-preflight    # NON-inference checks of the real isolated Codex (version, login, config, features, MCP, model)
python -B -m unittest discover -s tests   # full test suite (146 tests, ~40 s; all CLI output simulated)
```

## Browser live viewer (watch the real Codex <-> Claude exchange)
A chat-style page on **127.0.0.1 only**; no GUI framework, no server install, no dependencies.

**Open it**
```
python orchestrator.py run --project <dir> --task "..." --agents live    # interactive runs open the browser automatically
python orchestrator.py serve                      # latest task (running or saved); live-updates, Replay button
python orchestrator.py serve --task-id <id>       # a specific saved task
python orchestrator.py --web demo --delay 3       # watch a paced mock two-cycle task update live
```
Flags (global, before the subcommand): `--web` / `--no-web`, `--no-open` (print the URL, do not launch a browser),
`--hold-viewer SECONDS` (keep serving after the task ends), `--port N`. The URL is always printed:
`LIVE VIEWER (localhost only): http://127.0.0.1:<port>/?t=<token>&task=<id>`. Config: `viewer.web`, `viewer.auto_open_browser`, `viewer.port`.
Non-interactive callers (tests, scripts that pass their own `out`) do not start it unless `--web` is given. For a one-shot harness script:
`web_viewer.start_for_task(runs_dir, task_id)` before the first message.

**Conversation UX v2 (Hebrew-first, RTL)**: two modes. *תצוגת שיחה* (default) shows only the two AI participants — Codex (מנהל ומבקר) and Claude (מתכנת) — with the task panel on top, Codex's work plan collapsible, verification results and system events as compact rows, a status panel (run state, who is working, step, cycle) and a deterministic outcome label (e.g. *Codex אישר על סמך קריאה בלבד — לא הורץ* vs *יושם בפרויקט ואומת — הבדיקות הורצו ועברו*). Model text is shown verbatim; Hebrew appears only as fixed UI labels. An optional `user_summary_he` field in a future agent reply is shown separately and labelled as a summary. *תצוגה טכנית* shows the complete original record: every message with sender/recipient, cycle, timestamp, exact text, formatted JSON, meta (delivered prompts), orchestrator events, model metadata and saved raw CLI envelopes. Run state comes from the server: *ריצה פעילה* only while the task holds its workspace lock (or a harness transcript is still being written); otherwise *שיחה שמורה*; *הפעלה חוזרת* during replay. The connection indicator never implies that agents are working. Code is rendered without font ligatures so `->`/`!=` appear exactly as written.

**Conversation UX v2.1 — Hebrew summaries and evidence-based status.**
- *`user_summary_he`*: an optional field in the Codex plan/review and Claude report schemas. The agent writes 1-3 plain-Hebrew sentences in the SAME call (no extra model call). Codex's strict schema lists it as required but allows `""`; for Claude it is optional. The orchestrator validates it (string, at most 1500 chars); an invalid one is ignored and logged, never fatal. It is display-only: never forwarded to the other agent and never used for a decision.
- *Labels*: the viewer marks it "תקציר בעברית שכתב <agent> בעצמו". Messages without it (all runs so far) get a deterministic "תיאור המערכת · לא נכתב ע״י <agent>" line derived from the record; no Hebrew summary is ever invented.
- *Conversation Mode*: code and proposed tests are collapsed behind "הצג קוד" / "הצג בדיקות". "הודעה מקורית" shows the exact stored text.
- *Result card*: there is one result card, at the end of the conversation. Its evidence checklist covers planning, code proposed, code written to the project (measured diff), static check, automated tests (ran and passed / ran and failed / not run), Codex review, and implementation verified. Nothing turns green without recorded evidence.
- *Sidebar*: narrower and collapsible ("הסתר פאנל"). It shows the run state, who is working (live only), the current step and a per-cycle summary.
- *Titles and MOCK labels*: titles come from the task text, and MOCK runs are labelled. A run counts as MOCK only with positive evidence: all `state.agents` are Mock* classes, or it used the `demo` workspace.
- *Example*: `python orchestrator.py demo --he-summaries` creates a clearly-labelled MOCK example of what Hebrew summaries will look like. No model is called.

**What you see (v1 description, still accurate for the technical mode)**: USER / CODEX (planner+reviewer) / CLAUDE (executor) / ORCHESTRATOR (system) / TEST HARNESS as distinct chat identities;
every recorded message in order, unsummarised, with sender -> recipient, kind, cycle, time; header with task status, cycle, and the
**active agent** ("Codex is working...", "Claude is responding...", "Orchestrator is verifying...") taken from real `agent_start`/`agent_end`
events; CLI progress notes (`session started`, `reply received`, ...) as grey lines (toggle). Badges separate **PROPOSED CODE - NOT EXECUTED**,
**TESTS EXECUTED: passed/failed**, **STATIC CHECK ONLY**, **REVIEW: COMPLETE/CONTINUE/...**, errors and human-approval events. Each message can
be expanded, copied (exact stored text), shown **Raw**, and (Claude instructions) shows the full delivered prompt. **Replay** re-plays the whole
conversation at selectable speed; the task selector switches between saved runs. No refresh needed (Server-Sent Events, auto-reconnect).

**Honest streaming**: Codex `exec --json` and Claude `-p --output-format json` deliver a reply only when it is complete, so each message
appears when the orchestrator receives it (marked "delivered complete"); nothing is shown as partial text. A future adapter that really streams
can set `delivery: streamed` and will be marked as such.

**Security**: loopback bind only; random per-launch token required on every request; `Host` header must be localhost (blocks DNS rebinding and
other web pages); GET only; strict CSP (no external resources, nonce'd inline script); task ids validated twice against the `runs/` directory;
agent text is inserted with `textContent` only (a test page with `<script>`/`<img onerror>` stayed inert); stored text is passed through the
redactor again before it is served. Any other local process of the same user that learns the URL can read the conversation.

## Watching the conversation in the terminal
Every message delivered between the parties is shown **as it happens** and saved to `runs/<task-id>/transcript.jsonl`.
Parties: `user`, `codex`, `claude`, `orchestrator` (the verifier/relay). Each entry shows sequence number, cycle, UTC time,
**sender -> recipient**, kind and the message text:

```
+-- #6  cycle 1  2026-10-11 23:58:43Z  CODEX -> CLAUDE  [review]
| verdict: CONTINUE
| findings:
|   - add() subtracts
| next_instruction: add() returns a - b; it must return a + b.
+--
```
Kinds: `task`, `plan`, `instruction`, `report`, `verification`, `review`, `notice`, `approval_request`, `approval_decision`,
`control`, `malformed_output`, `final`. A full example (mock agents) is in `docs/sample_mock_conversation.txt`.

- **Exact messages, not summaries.** The transcript stores the exact text that was exchanged (only credentials are redacted).
  The screen shows JSON in a readable block layout; `conversation --json` prints the stored originals.
- **Honest delivery timing.** A final Codex/Claude reply is only meaningful when complete, so it is shown when it arrives
  (`delivery: complete`). While a real Codex call runs, its streamed CLI events appear immediately as dim `..` activity lines
  (`session started`, `working`, `reply received (N chars)`, `finished (tokens ...)`). Activity lines are progress notes, not
  messages; they are not in the transcript (they go to `events.jsonl`). Nothing is displayed as streamed that wasn't.
- **No hidden reasoning, no credentials.** Reasoning items from the CLI are never displayed or stored; secrets are redacted
  before display and storage; terminal control characters in agent text are stripped.
- **Attribution is literal.** If the orchestrator itself writes a corrective instruction (e.g. a rejected `COMPLETE`), it is shown as
  `ORCHESTRATOR -> CLAUDE`, not as Codex. Errors of an executor are `notice`s from the orchestrator, never fake Claude messages.
- The viewer is agent-neutral: a future Claude adapter only has to return its raw reply; attribution and display come from the
  orchestrator. `run`/`resume`/`demo` show the conversation by default; `--quiet` hides it (it is still saved).

```
python orchestrator.py conversation [--task-id ID] [--cycle N] [--message SEQ] [--json]   # inspect saved messages
python orchestrator.py replay       [--task-id ID] [--cycle N] [--delay 0.5]              # replay in original order
python orchestrator.py watch        [--task-id ID]            # second terminal: follow a running task live
python orchestrator.py status                                  # includes message count; global flags: --width N, --no-color, --show-prompts
```

## Commands
```
python orchestrator.py start                      # everyday use: asks for the task, you type "כן", the browser opens (native profile)
python orchestrator.py start --task "..." [--project DIR] [--max-cycles 3]
python orchestrator.py start --task-file spec.md  # a long task (UTF-8 file) instead of the command line
python orchestrator.py start --resume [TASK_ID]   # continue a paused / waiting task (asks again)
python orchestrator.py start --restricted --allow calc.py --allow "tests/*.py"   # optional no-tools, static-only profile
python orchestrator.py allow-tool [--rule "Bash(npm test)"] [--deny]            # decide on Claude's pending permission request
python orchestrator.py run --project <dir> --task "..." --agents live --accept-local-execution   # behind the live.enabled gate
python orchestrator.py demo --proposals           # MOCK walk-through of controlled file creation (no model is called)
python orchestrator.py serve [--task-id ID]       # reopen the browser viewer for any saved run (replay)
python orchestrator.py run --project <dir> --task "<text>" --agents mock --scenario <file.json>
python orchestrator.py run --project <dir> --task "<text>" --dry-run      # no agents, no files, no processes
python orchestrator.py status  [--task-id ID] [--json]
python orchestrator.py pause   [--task-id ID]     # takes effect between steps
python orchestrator.py resume  [--task-id ID] --scenario <file.json>
python orchestrator.py stop    [--task-id ID]     # also cancels a running verification process
python orchestrator.py approve --approval-id a1 [--deny] [--task-id ID]
```
`--task-id` defaults to the most recent task.

**Exit codes / final statuses**
- 0 `COMPLETE`: the deterministic gate and the reviewer both accept.
- 1 `FAILED`
- 2 usage or config error
- 3 `HUMAN_REQUIRED`
- 4 `PAUSED`: Ctrl+C or `pause`; resumable, and an interrupted write is recovered first.
- 5 `STOPPED`
- 6 `NEEDS_REVISION`: out of cycles or model-call budget, without acceptance.
- 7 `BLOCKED`: authentication, quota or configuration; the run stops immediately and there are no retries.

**Budgets**
- `max_codex_calls` / `max_claude_calls` limit model calls per run. They are counted before each call and persisted, so a resume cannot reset them.
- `start` uses 1 + cycles calls for Codex and one per cycle for Claude. A scenario file is JSON: `{"planner":[...], "executor":[...], "reviewer":[...]}`
(see `mock_scenarios.py` for the step format).

## What happens in a cycle
1. Planner returns plan + acceptance criteria + first instruction (JSON, validated).
2. Executor runs. The orchestrator snapshots the workspace before/after (sha256 of every file, including
   untracked, modified and deleted ones; plus `git status` if the workspace is a repo).
3. The orchestrator itself runs the configured verification command (policy-checked, scrubbed env, timeout).
4. Reviewer receives the evidence (not just the executor's claim) and returns `COMPLETE | CONTINUE | HUMAN_REQUIRED | FAILED`.
5. **`COMPLETE` is accepted only if** the verification passed, at least `min_tests_ran` tests ran, and (by default) files changed
   since the task started. Otherwise it is downgraded to `CONTINUE` and logged as `completion_rejected`.

Stops: `max_cycles` (8), `max_no_progress` (2 consecutive cycles with no workspace change and unchanged verification result),
malformed agent output, auth error, quota error, policy violation / approval needed (`HUMAN_REQUIRED`), user stop.

## Claude Code adapter (implementation agent)
- Uses the **existing subscription login** (`claude auth status` must show `claude.ai` / `firstParty`; API-key/console/bedrock logins
  fail the preflight). The CLI version is pinned (`claude.pinned_version`, now 2.1.293) and every required flag is looked up in the
  installed `claude --help` before use; if one is missing the adapter refuses to run.
- Every call is a fresh process: `claude -p --output-format json --json-schema <report schema> --permission-mode dontAsk
  --permission-prompts none --safe-mode --restricted --strict-mcp-config --disable-slash-commands --tools <list> --allowedTools <list>
  --disallowedTools <list>` plus `--session-id <new uuid>` (first call) or `--resume <uuid>` (later calls, flags re-sent every time).
  The prompt goes on **stdin**; nothing from model output ever reaches the command line. Flags come only from validated config.
  Never used: `--bare` (forces API-key auth), `--dangerously-skip-permissions`, `acceptEdits`/`auto`/`bypassPermissions`,
  `--add-dir`, `--settings`, `--mcp-config` (an AST test proves the forbidden strings appear only in the deny-list).
- Default tools: `Read, Glob, Grep, Edit, Write` and `Bash(python -m unittest *)`; web tools and `rm`/`del`/`git push`/`curl`/`pip` are
  denied; config validation rejects broad entries (`Bash`, `Bash(*)`, `WebFetch`, `mcp__*`, `*`) and shell-significant characters.
- **Session identity** comes from the CLI's own JSON envelope (validated as a UUID) and overrides any `session_id`/`turns` the model
  writes. A stored non-UUID id is refused instead of being passed on.
- **Errors**: subscription/usage-limit and auth failures stop the task immediately (no retry); timeouts kill the process tree and are retried
  as a no-progress cycle; malformed envelopes/reports stop the task; a **permission denial** in the envelope stops with `HUMAN_REQUIRED`
  and never widens anything; a stop request cancels the running CLI call.
- **What you see**: the Codex instruction as a `codex -> claude [instruction]` message (the full prompt actually delivered is stored in
  the message `meta` and shown with `--show-prompts`), progress lines while the call runs, then the report as `claude -> codex [report]`.
  The JSON output mode delivers the reply at completion, so it is displayed then (not pretended to be streamed).
- Replies are untrusted: unknown/permission-like fields are ignored and logged; the orchestrator compares claimed changes with the real
  file diff and runs its own verification before any `COMPLETE` is accepted.
- **Live gate**: `--agents live` needs `live.enabled: true` AND an owner-created approval file with BOTH `"codex_planner_reviewer": true`
  and `"claude_executor": true`. Neither exists today; without them nothing is started.

### What this configuration cannot enforce (human decision needed before live operation)
No OS-level sandbox exists: the lock-down flags are Claude Code's own permission system. Agent-written code run by the allowed
`python -m unittest` executes with your user rights (files outside the workspace and network access are not controlled or detected);
writes to protected files inside the workspace and deletions by such code are only detected afterwards; there is no `--max-turns` flag, so
turn limits are checked after the call (the timeout is the only hard bound); and the JSON envelope shape is assumed until the first live
call. `python orchestrator.py claude-preflight` prints this list. Use a disposable workspace for any first live test.

## Codex adapter (planner + reviewer)
- Executable: `toolchain/codex/node_modules/.bin/codex.cmd`, pinned **0.162.1**; `CODEX_HOME=~/.codex-orchestrator-home`
  (own ChatGPT login; the global Codex config/auth are untouched).
- Fixed invocation per call: `codex exec --ephemeral --skip-git-repo-check -C <fresh empty temp dir> -s read-only -m gpt-6-sol
  -c model_reasoning_effort="low" --json --output-schema <strict schema> -o <file> -` with the prompt on stdin, 120 s timeout,
  process-tree kill, cooperative cancel. **No `resume`, no `--search`, no `service_tier`, no Fast.**
- **Fail-closed preflight** before the first call (non-inference commands only): executable is inside the project toolchain, pinned
  version, `Logged in using ChatGPT` (and no API-key wording), `config.toml` has no `service_tier` anywhere, `forced_login_method="chatgpt"`,
  `sandbox_mode="read-only"`, `approval_policy="never"`, `[skills] include_instructions=false`, no MCP servers, every feature in
  `codex.required_disabled_features` reports `false`, the model is in the catalog, and the child environment contains no
  API-key/token/OPENAI/ANTHROPIC variables. Any doubt → `configuration_error`, no inference.
- Output handling: JSONL events are parsed; any tool-like item (`command_execution`, `mcp_tool_call`, ...), any service-tier indicator,
  a missing `turn.completed`, a non-zero exit, or an error event aborts. Errors are classified as auth / quota / config / timeout /
  malformed; auth, quota, config and malformed stop the task (no automatic retry). Output is then validated by the same strict
  validators as the mock agents. Prompts are redacted and size-limited; executor output is placed inside an `<untrusted_data>` block.
- **Two-part live gate** for `--agents hybrid`: `live.enabled: true` in the config **and** an owner-created approval file
  (`live.approval_file`, default `live_approval.json`, git-ignored) containing `{"codex_planner_reviewer": true}`. Neither exists today.
- Codex never sees the project workspace or its path: only the prompt text. That is a property of this configuration, not a claim that the
  sandbox hides other folders (it does not: a read-only sandbox can still read them; the shell tool is disabled instead).

## Phase 4d: controlled file creation (proposal mode)
Enabled per task by the OWNER's allowlist, for example:
`run --project <dir> --task "..." --allow-create calc.py [--allow-overwrite PATH] [--require-def add]`
To see it with MOCK agents: `python orchestrator.py demo --proposals [--delay 3]`.

**How a cycle works**
1. Codex's planner receives the allowlist as part of the task.
2. The executor gets **no file or command tools**. MOCK executors receive `NoToolsFS`; the real Claude adapter refuses proposal mode unless `claude.tools` and `allowed_tools` are `[]`, and then sends a `files` JSON schema. Claude returns `{"files": [{"path", "content"}], ...}`.
3. The orchestrator validates the whole proposal before writing anything (`proposals.ProposalApplier.validate`). One bad entry rejects all of it. Rejected:
   - traversal, absolute, drive, UNC, `:`/ADS, device names and 8.3 `~1` names;
   - paths not in the allowlist;
   - protected files (they can never be allowlisted);
   - symlinks or junctions on the path;
   - directories, unexpected overwrites, duplicates and deletions;
   - oversize, NUL or credential-like content.
4. Accepted files are written by `GuardedFS.write_atomic`:
   - a journal is written first, listing the files and the folders to be created;
   - each file goes to an exclusive temp file, then `fsync`, then `os.replace`;
   - path and link checks run before and after the write;
   - every file is read back and its SHA-256 is compared with the proposal.
5. **Interrupted writes.** An ordinary error rolls everything back: created files are removed, overwritten files are restored from backups, and new empty folders are removed. A crash leaves the journal `pending`; `resume` completes it if every file is on disk with the expected hash, and otherwise rolls it back, before anything else happens. It is shown as "שחזור אחרי כתיבה שנקטעה".
6. **Static verification only; generated code is never executed.** The checks are: content hash on disk equals the applied proposal, Python syntax via `ast.parse` (no import, no exec), and the required top-level definitions.
7. **Codex reviews evidence, not claims.** It gets the `file_apply` record (applied / rejected / failed, with reasons), the measured diff and the static evidence including the on-disk content. Claude's report is passed separately as untrusted.
8. **Completion gate.** COMPLETE requires a passing static check and actual changes. Any workspace change that is not one of the files the orchestrator just wrote halts the task ("changed outside the accepted proposal").

**Viewer.** New rows: "המערכת כתבה N קבצים לסביבת העבודה המבודדת", "ההצעה נדחתה — שום קובץ לא נכתב" (with the recorded reasons), "הכתיבה נכשלה — כל השינויים בוטלו", and per-file "זהה להצעה" / "תחביר Python תקין". The new phases are "המערכת בודקת וכותבת את הקבצים" and "המערכת בודקת את הקבצים (בלי להריץ)". The result card stays honest: *אומת סטטית בלבד — הקוד לא הורץ*, and "יישום אומת בפועל" stays "לא" until tests are executed.

**Remaining risks — this is a write gateway, not an OS sandbox.**
- *Path race (TOCTOU).* Another local process running as the same user could swap a folder for a junction between a check and the write. The repeated checks and `O_EXCL` temp files narrow this window but cannot close it. Windows has no `O_NOFOLLOW` for directories.
- *No OS isolation for the agent process.* A real Claude CLI process runs as your user. With `--tools ''` it has no file tools, but nothing at the OS level stops it, so a misbehaving CLI would be detected afterwards (snapshot diff), not prevented. Changes outside the workspace cannot be detected at all.
- *Snapshot limits.* The snapshot covers the workspace minus `snapshot_ignore_dirs`.
- *Static checks are not proof of correctness.* A passing syntax check says nothing about behavior; executing tests on generated code needs a separately approved, isolated runner.
- *Credential detection.* Credential-like content detection is pattern-based (the redactor), not exhaustive.

## Files
| File | Role |
|---|---|
| `orchestrator.py` | lifecycle, cycle controller, completion gate, CLI |
| `config.py` | config loading/validation, workspace validation |
| `claude_adapter.py` | real Claude Code executor adapter, preflight, lock-down command line |
| `sim_claude.py` | simulated Claude CLI used by tests and `demo --claude-sim` |
| `codex_adapter.py` | real Codex planner/reviewer adapter, preflight, live gate |
| `taskctl.py` | task lookup, approvals, status, dry run |
| `conversation.py` | transcript store (agent-neutral message log) |
| `web_viewer.py`, `web/index.html` | localhost browser live viewer (server + single-page UI) |
| `proposals.py` | Phase 4d proposal mode: owner allowlist, all-or-nothing validation, journaled atomic apply + crash recovery, static (non-executing) evidence |
| `viewer.py` | terminal conversation viewer, replay, watch |
| `cli.py` | command-line interface |
| `safety.py` | redaction, env scrubbing, path/command policy, guarded file gateway, process runner, snapshots |
| `store.py` | atomic JSON state, JSONL event log, pause/stop control file, workspace locks |
| `agents.py` | adapter interface, strict output validation, mock adapters, live-mode refusal |
| `mock_scenarios.py` | fixtures and scripted scenarios (tests and `demo`) |
| `config.json` | limits and behavior (unknown keys are rejected) |
| `docs/` | `sample_mock_conversation.txt` (generated by `demo`) |
| `runs/<task-id>/` | `state.json`, `baseline.json`, `events.jsonl`, `transcript.jsonl` (created at run time, git-ignored) |

## Config (`config.json`)
`max_cycles`, `max_executor_turns`, `process_timeout_sec`, `verification_timeout_sec`, `max_no_progress`, `min_tests_ran`,
`require_changes`, `verify_command` (argv list, must match `command_allowlist`), `command_allowlist` (argv prefixes),
`protected_path_patterns`, `snapshot_ignore_dirs`, `deletes_require_approval`, `max_file_write_bytes`, `runs_dir`, `live.enabled`
(must stay false in Phase 2). `verify_command` defaults to `python -m unittest discover -s tests` run inside the project.

## Billing safety
- No API key is read, required or used. Nothing in this code calls any API.
- Every child process gets a **copy** of the environment with `OPENAI_API_KEY`, `CODEX_API_KEY`, `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `*_BASE_URL` overrides, anything containing `TOKEN/SECRET/PASSWORD/API_KEY/CREDENTIAL`, and `CLAUDE_CODE_*`
  removed. Your real/global environment is never modified; values are never logged (names only).
- The code never uses `--bare` (it would force API-key auth) or any permission-bypass flag; a test greps the source for this.
- The orchestrator never sets `service_tier`, and the Codex adapter aborts if any service-tier indicator appears. Fast/priority modes consume plan limits faster and are never enabled.

## Security status — be precise
**NATIVE profile (default)** — the same trust model as using Claude Code yourself.
- Claude Code's own permission system is the control: `--permission-mode dontAsk` plus `claude.allowed_tools` (Read, Glob, Grep, Edit, Write, `Bash(python -m unittest *)`, `Bash(python -m pytest *)`) and `claude.disallowed_tools` (web, `git push`, `rm`, `del`, `curl`, `pip`).
- `--restricted` confines file tools to the task folder; `--safe-mode` and `--strict-mcp-config` mean no user hooks, plugins, MCP or global CLAUDE.md.
- `--dangerously-skip-permissions`, `bypassPermissions`, `--bare` and `--add-dir` are never used; preflight and the tests check this.
- Anything else Claude tries is denied and shown to you. Only you can approve a rule, for one task.
  - Blanket shell access (`Bash`, `Bash(*)`) and destructive commands (`rm`, `del`, `format`, `git push`, `curl`, `pip`, ...) can never be approved.
  - Rules with commas or newlines are refused.
- **Not isolated.** Code and tests run on this PC under your Windows account. A command allowed by a rule (for example `python -m unittest`) can execute whatever the code does. That is the accepted risk of local development, exactly as with Claude Code itself.

**RESTRICTED profile (optional)**
- Claude has no tools. Only owner-allowlisted paths are written, by the orchestrator.
- Proposals are rejected all-or-nothing for traversal, links, protected files and unexpected overwrites.
- Writes are journaled and atomic, with crash recovery. Checks are static only, and nothing generated is executed.
- Any change the orchestrator did not make halts the task.

**Both profiles**
- Subscription logins only: no API keys, no Fast/priority tier, no retries. Auth or quota errors stop the run as `BLOCKED`.
- Per-run call budgets, persisted. Timeouts, cancellation (Ctrl+C pauses; resume) and process-tree kill.
- Secrets are redacted in logs, prompts and the viewer. Agent JSON is validated, and permission-like fields from agents are ignored and logged.
- A live run needs your typed confirmation in an interactive terminal, or the separate `live.enabled` + approval-file gate.

## Remaining limitations
- Messages appear when complete (the CLIs do not stream text). The viewer is read-only: stop or pause with Ctrl+C in the start window, or `orchestrator.py stop|pause`.
- In the native profile the permission request detail comes from Claude Code's `permission_denials` (tool name and input). If a CLI version reports less, you see only the tool name and can type the rule yourself with `allow-tool --rule`.
- The orchestrator's own test run uses `verify_command` (default `python -m unittest discover -s tests`) and needs at least one test to count as passed. Projects that use another test command need `verify_command` adjusted in config.json.
- Real quota/auth wording is matched by patterns; an unknown wording surfaces as a FAILED process error rather than BLOCKED.
