#!/usr/bin/env bash
# Runs one skill and ALWAYS publishes a classified result.json (see DESIGN-remote-results.md).
set -uo pipefail
# NOTE: bb_step_result.json, not result.json — the PDLC skills use result.json as their own
# in-repo handoff artifact, so the control plane keeps a separate, unambiguous name.
RESULT="${RESULT_DIR:-/out}/bb_step_result.json"
START=$(date +%s)
emit () {  # status, reason, [next]
  # Build the JSON with ONE jq call from environment variables. The previous version
  # interpolated several $(...) substitutions inside a heredoc: if any of them failed
  # (odd characters in a multi-KB agent summary), the file was left truncated and
  # unparseable — the engine then saw "no result" and classified the step
  # failed_incomplete even though the work had succeeded.
  BB_STATUS="$1" BB_REASON="${2:-}" BB_NEXT="${3:-}" \
  BB_RUN="${RUN_ID:-}" BB_STEP="${STEP_ID:-}" BB_ATTEMPT="${ATTEMPT_NO:-1}" \
  BB_SKILL="${SKILL:-}" BB_SUMMARY="${SUMMARY:-}" BB_ARTIFACT="${ARTIFACT:-}" \
  BB_COST="${COST_USD:-0}" BB_DUR="$(( $(date +%s) - START ))" \
  BB_COMMIT="${COMMIT_SHA:-}" BB_MODELS="${MODELS_USED:-}" BB_MODEL_REQ="${BB_MODEL:-}" \
  jq -n --argjson files "${FILES_JSON:-[]}" --argjson review "${REVIEW_JSON:-[]}" '{
        run_id: env.BB_RUN, step_id: env.BB_STEP,
        attempt_no: (env.BB_ATTEMPT | tonumber? // 1),
        skill: env.BB_SKILL, status: env.BB_STATUS,
        reason: env.BB_REASON,
        next: (if env.BB_NEXT == "" then null else env.BB_NEXT end),
        summary: env.BB_SUMMARY, artifact: env.BB_ARTIFACT,
        files: $files, review_files: $review, commit: env.BB_COMMIT,
        models_used: env.BB_MODELS, model_requested: env.BB_MODEL_REQ,
        cost_usd: (env.BB_COST | tonumber? // 0),
        duration_s: (env.BB_DUR | tonumber? // 0)
      }' > "$RESULT".tmp 2>/dev/null

  # Only move into place if it actually parsed — never leave a half-written result.
  if [ -s "$RESULT".tmp ] && jq -e . "$RESULT".tmp >/dev/null 2>&1; then
    mv "$RESULT".tmp "$RESULT"
  else
    # last-ditch minimal result so the engine always gets a verdict it can read
    printf '{"run_id":"%s","step_id":"%s","attempt_no":%s,"status":"%s","summary":"(summary unavailable)","files":[],"cost_usd":%s}\n' \
      "${RUN_ID:-}" "${STEP_ID:-}" "${ATTEMPT_NO:-1}" "$1" "${COST_USD:-0}" > "$RESULT"
    rm -f "$RESULT".tmp
  fi
}

fail () { emit "$1" "$2"; exit "${3:-1}"; }

# --- progress heartbeat -------------------------------------------------------
# The backend polls this file so a long-running stage shows liveness instead of silence.
PROGRESS="${RESULT_DIR:-/out}/progress.json"
progress () {  # phase, note
  printf '{"phase":"%s","note":"%s","elapsed_s":%d,"ts":%d}\n' \
    "$1" "${2:-}" "$(( $(date +%s) - START ))" "$(date +%s)" > "$PROGRESS" 2>/dev/null || true
}
heartbeat_loop () {   # keeps elapsed_s ticking while the agent works
  while true; do
    sleep 5
    progress "${CURRENT_PHASE:-working}" "${CURRENT_NOTE:-agent running}"
    # also print, so `docker logs` shows the container is alive even while the
    # agent itself is quiet (claude -p buffers until it finishes).
    echo "[$(( $(date +%s) - START ))s] ${CURRENT_PHASE:-working}: ${CURRENT_NOTE:-agent running}"
  done
}
progress init "starting up"
[ -n "${SKILL:-}" ] || fail failed_init "no SKILL provided" 2
[ -d /workspace ]   || fail failed_init "no workspace mounted" 2

# --- INIT (git mode): clone the run branch, creating it from the source branch on step 1 ---
# The run owns its branch exclusively and steps run sequentially, so there is no concurrent
# writer — clone, work, push is safe without merge handling. A step "counts" only when its
# push lands; a push failure fails the step so it retries from the last pushed state.
if [ "${BB_GIT_MODE:-0}" = "1" ]; then
  [ -n "${GIT_REMOTE_URL:-}" ] || fail failed_init "BB_GIT_MODE set but no GIT_REMOTE_URL" 2
  [ -n "${RUN_BRANCH:-}" ]     || fail failed_init "BB_GIT_MODE set but no RUN_BRANCH" 2
  # embed the token for non-interactive auth, then scrub it from the stored remote
  AUTH_URL="$GIT_REMOTE_URL"
  if [ -n "${GH_TOKEN:-}" ]; then
    AUTH_URL=$(printf '%s' "$GIT_REMOTE_URL" | sed -E "s#https://#https://x-access-token:${GH_TOKEN}@#")
  fi
  progress init "cloning ${RUN_BRANCH}"
  cd /workspace
  # Try the run branch first (steps 2+); if it doesn't exist yet, create it from source.
  if git clone --quiet --single-branch --branch "$RUN_BRANCH" "$AUTH_URL" repo 2>/dev/null; then
    echo "cloned existing run branch $RUN_BRANCH" | tee -a "${RESULT_DIR:-/out}/agent.log"
    NEW_BRANCH=0
  else
    SRC="${GIT_SOURCE_BRANCH:-main}"
    git clone --quiet --single-branch --branch "$SRC" "$AUTH_URL" repo \
      || fail failed_init "could not clone $GIT_REMOTE_URL @ $SRC" 3
    cd repo && git checkout -q -b "$RUN_BRANCH" && cd ..
    echo "created run branch $RUN_BRANCH from $SRC" | tee -a "${RESULT_DIR:-/out}/agent.log"
    NEW_BRANCH=1
  fi
  # scrub credentials from the remote so the token never sits in .git/config
  ( cd repo && git remote set-url origin "$GIT_REMOTE_URL" 2>/dev/null || true )
  # everything below expects the repo at /workspace; point at the clone
  ln -sfn /workspace/repo /workspace/_repo 2>/dev/null || true
  WORKDIR_REPO=/workspace/repo
else
  WORKDIR_REPO=/workspace
fi

# --- INIT: make skills discoverable where Claude Code looks (.claude/skills) ---
# The image ships skills at /skills; Claude loads project skills from $PWD/.claude/skills.
mkdir -p "${WORKDIR_REPO}/.claude"
[ -e "${WORKDIR_REPO}/.claude/skills" ] || ln -sfn /skills "${WORKDIR_REPO}/.claude/skills"

# --- INIT: install MCP config with runtime credentials substituted ---
# Written to $HOME/bb-mcp.json (ours alone, passed via --mcp-config) rather than
# /workspace/.mcp.json: the workspace
# is a host-seeded repo whose ownership we do not control, and the container runs as non-root,
# so writing there can silently fail. $HOME is ours by construction.
# Failures are reported, never swallowed — a missing MCP config is why Jira "isn't connected".
MCP_TARGET="${HOME:-/home/node}/bb-mcp.json"
MCP_STATUS="not attempted"
# Jira Cloud authenticates with the account EMAIL in the username slot; Jira Server uses a
# real username. Prefer JIRA_EMAIL when provided, else fall back to JIRA_USERNAME, so both
# deployment types work without changing the MCP config shape.
JIRA_USER_EFFECTIVE="${JIRA_EMAIL:-${JIRA_USERNAME:-}}"
if [ -n "${JIRA_URL:-}" ] || [ -n "${GH_TOKEN:-}" ]; then
  if [ -f /opt/mcp.json ]; then
    if JIRA_URL="${JIRA_URL:-}" JIRA_USERNAME="${JIRA_USER_EFFECTIVE}" \
       JIRA_API_TOKEN="${JIRA_API_TOKEN:-}" GH_TOKEN="${GH_TOKEN:-}" \
       jq '.mcpServers.atlassian.env.JIRA_URL=env.JIRA_URL
           | .mcpServers.atlassian.env.JIRA_USERNAME=env.JIRA_USERNAME
           | .mcpServers.atlassian.env.JIRA_API_TOKEN=env.JIRA_API_TOKEN
           | .mcpServers.github.env.GITHUB_PERSONAL_ACCESS_TOKEN=env.GH_TOKEN' \
           /opt/mcp.json > "$MCP_TARGET" 2>/tmp/mcp_err; then
      MCP_STATUS="written to $MCP_TARGET"
      # also drop a project-scoped copy if the workspace is writable (belt and braces)
      if [ -w "${WORKDIR_REPO:-/workspace}" ]; then
        cp "$MCP_TARGET" "${WORKDIR_REPO:-/workspace}/.mcp.json" 2>/dev/null \
          && MCP_STATUS="$MCP_STATUS + repo/.mcp.json"
      fi
    else
      MCP_STATUS="jq failed: $(cat /tmp/mcp_err 2>/dev/null | head -c 160)"
      echo "WARNING: jq substitution failed ($MCP_STATUS) — trying direct build" >&2
      # Build the config from scratch rather than depending on reading /opt/mcp.json.
      cat > "$MCP_TARGET" <<JSON
{
  "mcpServers": {
    "atlassian": {
      "command": "uvx",
      "args": ["mcp-atlassian"],
      "env": {
        "JIRA_URL": "${JIRA_URL:-}",
        "JIRA_USERNAME": "${JIRA_USER_EFFECTIVE}",
        "JIRA_API_TOKEN": "${JIRA_API_TOKEN:-}"
      }
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "${GH_TOKEN:-}" }
    }
  }
}
JSON
      if jq -e '.mcpServers | length > 0' "$MCP_TARGET" >/dev/null 2>&1; then
        MCP_STATUS="written directly to $MCP_TARGET (jq fallback)"
      else
        MCP_STATUS="FAILED: could not produce a valid MCP config"
        echo "ERROR: $MCP_STATUS" >&2
      fi
    fi
  else
    MCP_STATUS="FAILED: /opt/mcp.json missing from image"
    echo "ERROR: $MCP_STATUS" >&2
  fi
else
  MCP_STATUS="skipped (no JIRA_URL / GH_TOKEN in env)"
fi

# --- DIAGNOSTICS ---------------------------------------------------------------
# The container is destroyed after the run, so anything worth inspecting must be written
# to the mounted /out now. This block is what tells you WHY an MCP call was denied.
DIAG="${RESULT_DIR:-/out}/diagnostics.txt"
{
  echo "=== identity ==="
  echo "uid=$(id -u) user=$(id -un) HOME=${HOME:-unset}"
  echo "perm_flags_will_be: $( [ "$(id -u)" -ne 0 ] && echo '--dangerously-skip-permissions' || echo '--permission-mode acceptEdits (ROOT FALLBACK)' )"
  echo
  echo "=== credentials present (values redacted) ==="
  for v in JIRA_URL JIRA_USERNAME JIRA_API_TOKEN GH_TOKEN ANTHROPIC_API_KEY; do
    eval "val=\${$v:-}"
    if [ -n "$val" ]; then
      # last 4 chars only — lets you match the key against the '****UgAA' in a 401 without
      # exposing the secret. If these 4 don't match what you expect, the wrong key arrived.
      tail4=$(printf '%s' "$val" | tail -c 4)
      echo "  $v: SET (len=${#val}, ends ...$tail4)"
    else
      echo "  $v: MISSING"
    fi
  done
  if [ -n "${ANTHROPIC_BASE_URL:-}" ]; then
    echo "  ANTHROPIC_BASE_URL: $ANTHROPIC_BASE_URL (key must be valid for THIS endpoint)"
  fi
  echo
  echo "=== tooling ==="
  echo "claude: $(command -v claude || echo MISSING) $(claude --version 2>/dev/null)"
  echo "uvx:    $(command -v uvx || echo MISSING)"
  echo "npx:    $(command -v npx || echo MISSING)"
  echo "uvx runnable as this user: $(uvx --help >/dev/null 2>&1 && echo YES || echo NO)"
  echo
  echo "=== workspace writability ==="
  echo "/workspace writable: $( [ -w /workspace ] && echo YES || echo NO )"
  echo "/out writable:       $( [ -w "${RESULT_DIR:-/out}" ] && echo YES || echo NO )"
} > "$DIAG" 2>&1

{
  echo
  echo "=== MCP config ==="
  echo "status: ${MCP_STATUS:-unknown}"
  for f in "${HOME:-/home/node}/bb-mcp.json" /workspace/.mcp.json; do
    if [ -f "$f" ]; then
      echo "  $f: present, placeholders_remaining=$(grep -c '\${' "$f" 2>/dev/null || echo 0)"
      echo "    servers: $(jq -r '(.mcpServers // {}) | keys | join(",")' "$f" 2>/dev/null || echo unparseable)"
      echo "    jira_url: $(jq -r '.mcpServers.atlassian.env.JIRA_URL // "MISSING"' "$f" 2>/dev/null)"
    else
      echo "  $f: ABSENT"
    fi
  done
} >> "$DIAG" 2>&1

# --- CONTEXT (allowed statuses + gate flag) ---
ALLOWED="[]"; GATE_FOLLOWS="false"; MEANINGS=""
if [ -n "${CONTEXT_FILE:-}" ] && [ -f "$CONTEXT_FILE" ]; then
  ALLOWED=$(jq -c '.allowed_result_statuses' "$CONTEXT_FILE" 2>/dev/null || echo '[]')
  GATE_FOLLOWS=$(jq -r '.gate_follows' "$CONTEXT_FILE" 2>/dev/null || echo false)
  # Definitions of each outcome word, so the agent isn't guessing from bare labels.
  MEANINGS=$(jq -r '(.result_status_meanings // {}) | to_entries
                    | map("  - " + .key + ": " + .value) | join("\n")' \
             "$CONTEXT_FILE" 2>/dev/null || echo "")
  # A reviewer sent this step back — their notes are the point of the re-run.
  FEEDBACK=$(jq -r '.reviewer_feedback // ""' "$CONTEXT_FILE" 2>/dev/null || echo "")
  # Upstream hand-off: a prior step routed here on a non-happy verdict (e.g. test-verify
  # BLOCK -> implement). Surface why, and point at the report file it committed.
  HANDOFF_FROM=$(jq -r '.upstream_handoff.from_step // ""' "$CONTEXT_FILE" 2>/dev/null || echo "")
  HANDOFF_STATUS=$(jq -r '.upstream_handoff.status // ""' "$CONTEXT_FILE" 2>/dev/null || echo "")
  HANDOFF_SUMMARY=$(jq -r '.upstream_handoff.summary // ""' "$CONTEXT_FILE" 2>/dev/null || echo "")
  HANDOFF_FILES=$(jq -r '(.upstream_handoff.report_files // []) | join(", ")' "$CONTEXT_FILE" 2>/dev/null || echo "")
fi

# --- DEMO/MOCK MODE ---
if [ "${BB_MOCK:-0}" = "1" ]; then
  sleep "${BB_MOCK_SECONDS:-4}"
  case "${BB_MOCK_FORCE:-}" in
    crash) exit 137 ;;
    fail)  fail failed_execution "mock deterministic failure" 1 ;;
    block) SUMMARY="Tests are not honestly green." COST_USD=0.02 emit BLOCK "mock block" implement; exit 0 ;;
  esac
  ST="${STORY_ID:+ for $STORY_ID}"
  if [ "$GATE_FOLLOWS" = "true" ]; then
    SUMMARY="${SKILL} finished (mock)${ST}. Written for a reviewer: ready to approve."
  else
    SUMMARY="${SKILL} finished (mock)${ST}."
  fi
  ARTIFACT="docs/${SKILL}.md" COST_USD=0.0${RANDOM:0:2}
  emit completed "mock run"; exit 0
fi

# --- EXECUTE ---
# Build the prompt. If a STORY_ID is given, tell the skill to fetch it from Jira via MCP.
STORY_LINE=""
if [ -n "${STORY_ID:-}" ]; then
  STORY_LINE="The target story is ${STORY_ID}. Use the Atlassian (Jira) MCP to fetch its details
(summary, description, acceptance criteria) before you begin."
fi
# NOTE ON THE PROMPT: never name a result file here. This wrapper owns the control-plane
# result (bb_step_result.json); the agent only does the skill's work and reports an outcome
# word. An earlier version said "your result.json status must be..." and the agent inferred
# it should WRITE a result.json, colliding with the skills' own artifacts.
FEEDBACK_LINE=""
if [ -n "${FEEDBACK:-}" ]; then
  FEEDBACK_LINE="A reviewer sent your previous attempt back for revision. Their feedback:
---
${FEEDBACK}
---
Address this specifically."
fi
HANDOFF_LINE=""
if [ -n "${HANDOFF_FROM:-}" ]; then
  HANDOFF_LINE="You are being run because the '${HANDOFF_FROM}' step returned '${HANDOFF_STATUS}',
which routes here to be addressed.$( [ -n "${HANDOFF_FILES}" ] && printf ' Read its report first: %s (in the repo). Address every point it raises.' "${HANDOFF_FILES}" )
$( [ -n "${HANDOFF_SUMMARY}" ] && printf 'Its summary: %s' "${HANDOFF_SUMMARY}" )"
fi
PROMPT="Run the ${SKILL} skill. Follow ${WORKDIR_REPO}/.claude/skills/${SKILL}/SKILL.md exactly.
${STORY_LINE}
${HANDOFF_LINE}
${FEEDBACK_LINE}
When you are completely finished, judge the outcome of your own execution and end your
reply with a final line in exactly this form:
BB_OUTCOME: <one of ${ALLOWED}>

What each outcome means:
${MEANINGS}
Choose the one that honestly describes your execution. Produce the artifacts your skill calls
for first; the outcome word is your verdict on that work.

Also tell the reviewer which files are worth looking at. For each file a human should review,
add a line (before BB_OUTCOME) in exactly this form:
BB_REVIEW: <path relative to repo root> | <short reason>
List every file that matters to the review — the key source you wrote or changed, the report
to check against — not incidental or generated files. If your skill already records this in its
own hand-off doc, mirror the same set here. These become the reviewer's default file list.
Do not create or modify any file for the purpose of reporting that outcome — the line in your
reply is the only thing that is read.
$( [ "$GATE_FOLLOWS" = "true" ] && echo "A human reviewer will read your summary before the run continues — write your closing summary for them." )"

# Non-interactive. --permission-mode acceptEdits avoids edit prompts; MCP config is picked up
# from /workspace/.mcp.json. (Note: --dangerously-skip-permissions is blocked under root, so we
# rely on acceptEdits + a pre-trusted workspace.)
# --- permission mode ---------------------------------------------------------
# The agent must run unattended: acceptEdits only auto-approves FILE EDITS, so Bash and
# MCP tool calls still block waiting for approval (which never comes in a container).
# --dangerously-skip-permissions grants all of them, but Claude Code refuses it under
# root — hence the non-root `agent` user in the Dockerfile.
if [ "$(id -u)" -ne 0 ]; then
  PERM_FLAGS="--dangerously-skip-permissions"
else
  echo "WARNING: running as root — Claude Code blocks --dangerously-skip-permissions." | tee -a "${RESULT_DIR:-/out}/agent.log"
  echo "         Bash/MCP tool calls will be denied. Rebuild the image so it runs as non-root." | tee -a "${RESULT_DIR:-/out}/agent.log"
  PERM_FLAGS="--permission-mode acceptEdits"
fi

# Point Claude Code explicitly at the MCP config we just wrote, rather than relying on
# it discovering a file by convention (which is what silently failed before).
# Point Claude Code at OUR config and nothing else. --strict-mcp-config is important:
# a seeded repo often ships its own .mcp.json, which would otherwise be merged or preferred,
# leaving the Jira server undefined ("no MCP tools exposed").
# Per-step model, passed by the backend as BB_MODEL. This is what actually enforces the
# model choice — without --model, Claude Code defaults to Opus regardless of any model
# named in the SKILL.md.
MODEL_FLAG=""
if [ -n "${BB_MODEL:-}" ]; then
  MODEL_FLAG="--model ${BB_MODEL}"
fi
echo "MODEL_FLAG=${MODEL_FLAG:-<none>}" >> "${RESULT_DIR:-/out}/diagnostics.txt" 2>/dev/null || true
echo "MODEL_PROFILE=${BB_ACTIVE_PROFILE:-anthropic} (resolved model: ${BB_MODEL:-default})" \
  >> "${RESULT_DIR:-/out}/diagnostics.txt" 2>/dev/null || true
if [ -n "${ANTHROPIC_BASE_URL:-}" ]; then
  echo "ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL} (vendor endpoint; needs ANTHROPIC_AUTH_TOKEN)" \
    >> "${RESULT_DIR:-/out}/diagnostics.txt" 2>/dev/null || true
fi

MCP_FLAG=""
# Only pass --mcp-config if the file exists AND actually parses as JSON with servers in it.
# Passing a path that failed to write gives "Invalid MCP configuration: not a valid JSON",
# which hides the real cause (the write failed earlier).
if [ -s "$MCP_TARGET" ] && jq -e '.mcpServers | length > 0' "$MCP_TARGET" >/dev/null 2>&1; then
  CLAUDE_HELP=$(claude -p --help 2>&1)
  if printf '%s' "$CLAUDE_HELP" | grep -q -- "--mcp-config"; then
    MCP_FLAG="--mcp-config $MCP_TARGET"
    printf '%s' "$CLAUDE_HELP" | grep -q -- "--strict-mcp-config" \
      && MCP_FLAG="$MCP_FLAG --strict-mcp-config"
  fi
else
  echo "WARNING: no usable MCP config at $MCP_TARGET — running WITHOUT MCP servers." >&2
fi
{
  echo
  echo "=== MCP readiness ==="
  echo "MCP_FLAG=${MCP_FLAG:-<none>}"
  # Prove the Atlassian MCP server can actually START before we depend on it. uvx has to
  # download mcp-atlassian on first use; if that fails (network/cache/permissions) the
  # server never comes up and Claude reports "no MCP tools exposed" with no further clue.
  if [ -n "${JIRA_URL:-}" ]; then
    if timeout 120 uvx mcp-atlassian --help >/tmp/mcp_probe 2>&1; then
      echo "uvx mcp-atlassian: STARTS OK"
    else
      echo "uvx mcp-atlassian: FAILED (rc=$?) — this is why Jira has no tools:"
      sed 's/^/    /' /tmp/mcp_probe | head -20
    fi
  fi
} >> "${RESULT_DIR:-/out}/diagnostics.txt" 2>&1 || true

CURRENT_PHASE=agent
CURRENT_NOTE="claude is working${STORY_ID:+ on $STORY_ID}"
progress agent "$CURRENT_NOTE"
heartbeat_loop & HB_PID=$!

# Stream the agent's output to the container log AND capture it, so `docker logs -f` shows
# progress live instead of the run being a black box. (Previously $(...) swallowed everything.)
LOGFILE="${RESULT_DIR:-/out}/agent.log"
echo "=== running ${SKILL}${STORY_ID:+ for $STORY_ID} ===" | tee -a "$LOGFILE"
set -o pipefail
cd "${WORKDIR_REPO:-/workspace}"

# `claude -p` buffers its whole answer and prints nothing until done, which makes a long
# run look hung. BB_STREAM=1 (default) uses stream-json so events appear as they happen.
if [ "${BB_STREAM:-1}" = "1" ] && claude -p --help 2>&1 | grep -q "output-format"; then
  STDBUF=""; command -v stdbuf >/dev/null 2>&1 && STDBUF="stdbuf -oL -eL"
  $STDBUF claude -p "$PROMPT" $PERM_FLAGS $MODEL_FLAG $MCP_FLAG \
      --output-format stream-json --verbose 2>&1 | tee -a "$LOGFILE"
  RC=${PIPESTATUS[0]}
else
  echo "(streaming unavailable — output will appear when the agent finishes)" | tee -a "$LOGFILE"
  claude -p "$PROMPT" $PERM_FLAGS $MODEL_FLAG $MCP_FLAG 2>&1 | tee -a "$LOGFILE"
  RC=${PIPESTATUS[0]}
fi
kill "$HB_PID" 2>/dev/null || true
OUTPUT=$(tail -c 4000 "$LOGFILE" 2>/dev/null || echo "")

# Distinguish WHICH credential failed, so "the step failed" isn't a mystery. These checks read
# the agent's own error output. Model-auth (Anthropic key) is the most common and shows up as a
# 401 with model "<synthetic>"; Jira/GitHub failures surface as MCP tool errors; git failures as
# push/clone errors (already handled in the COMMIT block).
if grep -qiE '401 Authentication|api key.*invalid|authentication_failed' "$LOGFILE" 2>/dev/null; then
  fail failed_execution "ANTHROPIC_API_KEY rejected by the model API (401). The key reaching the container is invalid, expired, or doesn't match ANTHROPIC_BASE_URL. This is NOT a Jira or git problem — it's the model credential." 1
fi
if grep -qiE 'mcp.*(unauthorized|401|invalid.token)|atlassian.*(401|forbidden)' "$LOGFILE" 2>/dev/null; then
  fail failed_execution "Jira/Atlassian MCP authentication failed — check JIRA_API_TOKEN / JIRA_USERNAME. (Model auth was fine.)" 1
fi

if [ "$RC" -ne 0 ]; then
  progress failed "agent exited $RC"
  fail failed_execution "agent exited non-zero ($RC): $(printf '%s' "$OUTPUT" | tail -c 400)" 1
fi
progress commit "agent finished, committing"

# --- COMMIT ---
# Capture WHICH files this step touched, so a reviewer at a gate can open the actual
# artifacts (story-design.md, test-plan.md, ...) rather than only reading the summary.
# git already knows this precisely — no need to invent a mechanism.
FILES_JSON="[]"
REPO="${WORKDIR_REPO:-/workspace}"
if [ -d "${REPO}/.git" ]; then
  cd "$REPO"
  # Never commit platform plumbing into the user's branch: the skills symlink and our MCP
  # config live in the working tree but must not land on their history.
  git rm -r --cached --quiet .claude 2>/dev/null || true
  rm -f .mcp.json 2>/dev/null || true
  printf '.claude/\n.mcp.json\n' >> .gitignore 2>/dev/null || true
  git add -A 2>/dev/null
  FILES_JSON=$(git diff --cached --name-status 2>/dev/null | \
    grep -v -E '^[A-Z][[:space:]]+\.claude/' | \
    grep -v -E '^[A-Z][[:space:]]+\.mcp\.json$' | \
    grep -v -E '^[A-Z][[:space:]]+\.gitignore$' | \
    jq -Rn '[inputs | split("\t") | select(length >= 2) |
             {status: .[0], path: .[-1]}]' 2>/dev/null || echo "[]")
  git -c user.email=agent@bheembhai -c user.name=BheemBhai \
      commit -q -m "${SKILL} (run ${RUN_ID:-} step ${STEP_ID:-})" 2>/dev/null || true
  COMMIT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "")

  # Git mode: push the branch. The step only "counts" once the push lands — so a push
  # failure fails the step (it will retry from the last pushed state). Re-embed the token
  # just for the push via an explicit URL so it never persists in .git/config.
  if [ "${BB_GIT_MODE:-0}" = "1" ]; then
    PUSH_URL="$GIT_REMOTE_URL"
    if [ -n "${GH_TOKEN:-}" ]; then
      PUSH_URL=$(printf '%s' "$GIT_REMOTE_URL" | sed -E "s#https://#https://x-access-token:${GH_TOKEN}@#")
    fi
    progress publish "pushing ${RUN_BRANCH}"
    if git push --quiet "$PUSH_URL" "HEAD:${RUN_BRANCH}" 2>>"${RESULT_DIR:-/out}/agent.log"; then
      echo "pushed to ${RUN_BRANCH} (${COMMIT_SHA})" | tee -a "${RESULT_DIR:-/out}/agent.log"
    else
      fail failed_infra "git push to ${RUN_BRANCH} failed — will retry from last pushed state" 4
    fi
  fi
else
  echo "WARNING: no .git in workspace — no commit/push; file list is mtime-based." >&2
  NOGIT=1
  FILES_JSON=$(find "$REPO" -newermt "@$START" -type f \
      -not -path '*/.git/*' -not -path '*/.claude/*' -not -path '*/node_modules/*' \
      -not -path '*/.venv/*' -not -path '*/__pycache__/*' -not -path '*/.pytest_cache/*' \
      2>/dev/null | head -50 | \
      sed "s|^${REPO}/||" | jq -Rn '[inputs | {status:"M", path:.}]' 2>/dev/null || echo "[]")
fi
# Extract a human-readable summary. With --output-format stream-json the log is
# line-delimited JSON, so pull the final result text rather than raw JSON.
#
# IMPORTANT: never slurp the whole log into a shell variable — a real run produces
# megabytes of stream-json, and $(...) plus `jq -rs` on that can exhaust the container's
# memory limit and get the process OOM-killed *after* the agent succeeded but *before*
# the result is published. Stream through files and read only the tail.
progress summarize "extracting result"
SUMMARY_FILE="${RESULT_DIR:-/out}/summary.txt"
: > "$SUMMARY_FILE"

# The final "result" event is the last line of a completed stream — search backwards
# through a bounded tail rather than parsing the entire file.
tail -n 50 "$LOGFILE" 2>/dev/null | grep '^{' | \
  jq -r 'select(.type=="result") | .result // empty' 2>/dev/null | tail -1 > "$SUMMARY_FILE" || true

if [ ! -s "$SUMMARY_FILE" ]; then
  tail -n 200 "$LOGFILE" 2>/dev/null | grep '^{' | \
    jq -r 'select(.type=="assistant") | .message.content[]? | select(.type=="text") | .text' \
    2>/dev/null | tail -1 > "$SUMMARY_FILE" || true
fi
if [ ! -s "$SUMMARY_FILE" ]; then
  tail -c 1000 "$LOGFILE" > "$SUMMARY_FILE" 2>/dev/null || true
fi

# real cost reported by the agent (also from the tail, not the whole file)
RC_COST=$(tail -n 50 "$LOGFILE" 2>/dev/null | grep '^{' | \
          jq -r 'select(.total_cost_usd != null) | .total_cost_usd' 2>/dev/null | tail -1)
case "$RC_COST" in ''|*[!0-9.]*) : ;; *) COST_USD="$RC_COST" ;; esac

# Record the model(s) actually used, so "did the right model run?" is answerable from the
# result without digging through logs. This is the accountability half of model governance:
# we enforce via --model, and we record what actually happened.
MODELS_USED=$(tail -n 50 "$LOGFILE" 2>/dev/null | grep '^{' | \
          jq -r 'select(.modelUsage != null) | .modelUsage | keys | join(",")' 2>/dev/null | tail -1)
[ -n "$MODELS_USED" ] && echo "MODELS_USED=$MODELS_USED (requested: ${BB_MODEL:-default})" \
    >> "${RESULT_DIR:-/out}/diagnostics.txt" 2>/dev/null || true

# The agent reports its outcome as a BB_OUTCOME line in its reply (never as a file).
STATUS=$(grep -oE 'BB_OUTCOME:[[:space:]]*[A-Za-z_]+' "$SUMMARY_FILE" 2>/dev/null | tail -1 \
         | sed -E 's/.*BB_OUTCOME:[[:space:]]*//')
[ -n "$STATUS" ] || STATUS="completed"

# The skill declares which files a human should actually review via BB_REVIEW lines:
#   BB_REVIEW: path/to/file.py | short note on why
# We turn each into {path, note}. This is the curated list the UI shows by default instead
# of every git-touched file. If the skill emits none, review_files stays [] and the UI falls
# back to the full changed-file list — so nothing is ever hidden by omission.
REVIEW_JSON=$(grep -oE 'BB_REVIEW:[[:space:]]*.+' "$SUMMARY_FILE" 2>/dev/null | \
  sed -E 's/^BB_REVIEW:[[:space:]]*//' | \
  jq -Rn '[inputs
           | split("|")
           | {path: (.[0] | gsub("^\\s+|\\s+$";"")),
              note: (if length > 1 then (.[1] | gsub("^\\s+|\\s+$";"")) else "" end)}
           | select(.path != "")]' 2>/dev/null || echo "[]")
[ -n "$REVIEW_JSON" ] || REVIEW_JSON="[]"

# don't show the machine-readable lines to a human reviewer; cap the length
SUMMARY=$(sed -E '/BB_OUTCOME:[[:space:]]*[A-Za-z_]+/d; /BB_REVIEW:[[:space:]]*/d' \
          "$SUMMARY_FILE" 2>/dev/null | tail -c 1500)
progress publish "writing result"
emit "$STATUS" "ok"; exit 0
