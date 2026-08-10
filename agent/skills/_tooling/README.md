# _tooling — token cost capture

`story_tokens.py` computes **per-model** token usage and estimated cost for one `story-implement`
run, for posting to Jira (`customfield_10105` = total cost USD, plus a per-model breakdown comment).

## Clean-slate reset (`otel_reset.py`) — runs FIRST in story-implement
`otel_reset.py` runs as **precondition step 0** of `story-implement`. It establishes a clean cost
boundary so the story's captured cost reflects the story work, not the generic pre-story chatter
(which story to pick, AC review, tangents) earlier in the session. It:
1. kills any running `otel_receiver.py`,
2. archives the existing `otel-costs.json` to a timestamped `.bak.json` (recoverable; old data
   used pre-fix aggregation or belongs to earlier discussion),
3. starts a fresh receiver stamped with `RECEIVER_VERSION`.

Dropping the pre-reset intervals is intentional noise-removal, not data loss. Capture from the
reset point forward is predominantly the story. Caveat: long *unrelated* mid-story tangents still
count; keep detours short for the cleanest figure. `RECEIVER_VERSION` also means a future change to
the receiver's aggregation auto-invalidates stale data (bump the constant), so this class of bug
can't silently recur. Run manually: `uv run python otel_reset.py` (or `--no-restart`).

## Why a wrapper (not the skill)
A skill is instructions the model follows — it cannot read its own token usage. That data lives in
the Claude Code session logs (`~/.claude/projects/<encoded-path>/<session>.jsonl`) and the client.
So measurement happens here, in a script, and `story-implement` consumes the number.

## What it does
- Reads the session JSONL, **dedupes by requestId** (one request writes several JSONL lines), and
  groups `usage` **per model** (opus / sonnet / haiku).
- Prices each model's tokens at **that model's own rate** (never blended), with cache adjustments
  (cache-read ~10% of input, cache-creation ~125%), and sums to a total.
- **Auto-located session (Stop hook):** `capture_session.py` is registered as a Claude Code Stop
  hook (`.claude/settings.json`). It fires at the end of every turn and writes `session_id` +
  `transcript_path` to `.claude/last-session.json`. `story_tokens.py` reads this file first —
  no `--session-id` or `--project-path` needed. Run the script bare and it finds the session.
- **Default source: JSONL** — always present, always written. Provides per-model totals plus
  turn-level data for per-phase and cost-driver breakdown.
- **Optional: statusbar** (`--statusbar-json`) — only available if you manually export the Claude
  Code statusbar token counter to a file at session end. Includes extended-thinking tokens so it
  is billing-exact. When provided it overrides the per-model totals; JSONL turn data still
  provides the per-phase and cost-driver breakdown alongside it. Omit if not captured.

## Source priority (highest to lowest)

| Source | How | Accuracy | Setup needed |
|--------|-----|----------|-------------|
| **OTEL** (`otel-costs.json`) | `otel_receiver.py` running as background process | Billing-exact — `claude_code.cost.usage` metric includes thinking tokens; same source as `/usage` | One-time: add OTEL env to `~/.claude/settings.json`, run receiver |
| **Statusbar JSON** (`--statusbar-json`) | Manual export from Claude Code `/usage` view | Billing-exact | Manual per-session |
| **JSONL** (auto via Stop hook) | `capture_session.py` Stop hook → `.claude/last-session.json` | Billing-accurate — `output_tokens` already includes thinking tokens per Anthropic API spec | Zero setup (always runs) |

## Session isolation

Story cost data must only include tokens from the story's own session. Two tiers:

**Tier 1 — automatic (normal path):** `story_tokens.py` runs at Step 6.5 *within* the
story-implement session. The Stop hook (`capture_session.py`) fires at the end of every turn
and writes `.claude/last-session.json` with the current `session_id`. When Step 6.5 reads this
file, it is always the story's own session — no other session can interleave during a single
model turn.

**Tier 2 — manual (post-session runs):** Running the script manually after the session has
ended (e.g. the next day) is a legitimate use-case. But picking "the latest session" globally
would silently attribute unrelated work to the story. The script **refuses** in this case:

```
ERROR: session cannot be auto-located — run within story-implement (Stop hook writes
.claude/last-session.json automatically) or pass --session-id <id> or --project-path <repo>
```

Get the session id from the Claude Code session list or from `.claude/last-session.json`
immediately after the story (before switching to another session):

```bash
cat .claude/last-session.json   # → {"session_id": "abc123", "transcript_path": "..."}
python .claude/skills/_tooling/story_tokens.py --story LNPRTL-33 --session-id abc123
```

## Per-story session lifecycle

Follow this sequence for clean, accurate cost attribution per story:

### 1. Before opening Claude Code — start the OTEL receiver (once per machine boot)

The OTEL receiver must be running BEFORE Claude Code starts, otherwise early-session metrics
are lost. Start it once and leave it running across all stories and sessions:

```bash
# Persistent background process — survives across multiple Claude Code sessions
# Run from the project root (learn-portal/)
nohup uv run python .claude/skills/_tooling/otel_receiver.py \
  > ~/.claude/otel-receiver.log 2>&1 &

# Verify it's running:
curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:4318/v1/metrics \
  -H "Content-Type: application/json" -d '{"resourceMetrics":[]}'
# → 200 means running; "Connection refused" means not started yet
```

**To stop the receiver** (e.g. when done for the day or to restart it):

```bash
pkill -f otel_receiver.py

# Or if you need the PID first:
ps aux | grep otel_receiver.py | grep -v grep
kill <PID>
```

The receiver writes `~/.claude/otel-costs.json` and accumulates data across sessions
without needing to be restarted between stories. If it crashes mid-story, `story_tokens.py`
falls back automatically to JSONL (still billing-accurate; you lose the per-skill OTEL
breakdown for that session only).

### 2. Open a fresh Claude Code session — one session per story

**Start a new Claude Code session for each story.** Never carry a previous story's context
into the next one:
- Pre-story turns inflate the story's cost attribution (the Stop hook sees ALL turns, not
  just the story's turns)
- Context from story N leaks into story N+1's reads, compounding cache-read costs

Open Claude Code in the project root, then invoke `story-implement`:
```bash
cd /path/to/learn-portal
claude   # opens a fresh session
# then: /story-implement LNPRTL-34
```

### 3. During the story — compact at the two prompts

`story-implement` will prompt you to run `/compact` at two points:
- **After `test-verify` PASS** — the TDD red/green loop accumulates the most throwaway
  context; compacting here before code-review saves the most tokens
- **After a `test-verify` BLOCK loop resolves** — drop the failed-attempt context

`/compact` is a Claude Code CLI command — type it in the prompt bar. After compacting,
tell Claude to continue and it resumes from the artifact-backed state (nothing is lost;
the next step re-reads the small artifact it needs).

### 4. After the PR is open — costs are recorded automatically

Step 6.5 of `story-implement` runs `story_tokens.py` and writes:
- `customfield_10105` (total cost USD) to the Jira story
- `ai_tokens_usage` (full JSON breakdown) to the Jira story

Then close the Claude Code session. The next story starts at step 1 with a fresh session.

### What if you need to re-run story_tokens.py after the session closes?

```bash
# Get the session id before closing (or read it right after):
cat .claude/last-session.json
# → {"session_id": "abc123", "transcript_path": "..."}

# Then run with explicit session id (Tier 2 — safe, no cross-session risk):
python .claude/skills/_tooling/story_tokens.py --story LNPRTL-33 --session-id abc123
```

## OTEL receiver details

### Required: env vars must be in `~/.bashrc` (not just `settings.json`)

Claude Code's OTEL SDK initializes inside the Node.js process at startup and reads env vars
from the **shell environment** (`process.env`). The `env` block in `~/.claude/settings.json`
only injects vars into bash tool subprocesses — it does NOT reach the Node.js OTEL SDK. If
the vars are only in `settings.json`, Claude Code starts up with no OTEL endpoint configured
and sends no metrics, even if the receiver is running.

**One-time setup per developer machine** — add to `~/.bashrc` (or `~/.zshrc`):

```bash
# Claude Code OTEL telemetry
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_METRIC_EXPORT_INTERVAL=10000
export OTEL_LOGS_EXPORT_INTERVAL=5000
```

Then open a fresh terminal (so `.bashrc` is sourced) before launching `claude`. Verify:

```bash
echo $OTEL_EXPORTER_OTLP_ENDPOINT   # → http://localhost:4318
```

The receiver listens on `localhost:4318` (HTTP/JSON OTLP) and writes
`~/.claude/otel-costs.json`, keyed by `session.id` + model family.

When the receiver is running, `story_tokens.py` automatically picks up OTEL data as
`source: "otel+jsonl"` — billing-exact cost with JSONL structural breakdown alongside.
If not running, source falls back to `"jsonl"` — still billing-accurate, no action needed.

## Accuracy note
The Anthropic API's `usage.output_tokens` already **includes** extended-thinking tokens — they
are a subset of `output_tokens`, not counted separately (confirmed in the Messages API reference
via `output_tokens_details.thinking_tokens`). JSONL records this field directly, so JSONL-derived
costs are billing-accurate. The three sources (OTEL / statusbar / JSONL) all ultimately read the
same API usage data; differences between them are due to **attribution scope** (which turns are
in scope for the story) not thinking-token inclusion.

## Usage
```bash
# simplest — Stop hook already wrote .claude/last-session.json; no flags needed
python .claude/skills/_tooling/story_tokens.py --story LNPRTL-30

# explicit session id overrides the auto-located session
python .claude/skills/_tooling/story_tokens.py --story LNPRTL-30 --session-id <uuid>

# or scope to a repo path (searches ~/.claude/projects/)
python .claude/skills/_tooling/story_tokens.py --story LNPRTL-30 --project-path /path/to/repo

# add the more-accurate statusbar total if you captured it manually
python .claude/skills/_tooling/story_tokens.py --story LNPRTL-30 --statusbar-json sb.json

# CLAUDE_ROOT overrides ~/.claude (e.g. in containers)
```
Output is JSON: `customfield_10105` (total cost USD), `total_tokens`, `per_model[]` (tokens + cost
each), `source` (jsonl|statusbar), and `caveat`.

## How story-implement uses it
At the In Review transition, `story-implement`:
1. runs this wrapper (no flags — Stop hook already wrote `.claude/last-session.json`),
2. writes `total_cost_usd` to **customfield_10105** (number field) via the Atlassian MCP,
3. writes the full JSON breakdown to **customfield_10138** (Paragraph field) via the Atlassian MCP
   — teams query this via JQL + REST to aggregate cost data across stories without parsing comments,
4. posts a human-readable per-model + per-phase + cost-driver summary as a Jira comment.
Best-effort: if the session can't be found or the MCP write fails, it's noted and the run
continues — the cost record isn't a gate.

## Editing pricing
Rates live in `RATES` at the top of `story_tokens.py` (USD per 1M tokens) plus `CACHE_READ_MULT`
and `CACHE_CREATE_MULT`. Update them if Anthropic pricing changes or you switch models. For Ollama/
local models (no per-token price), set those families' rates to 0 or add them to `RATES`.
