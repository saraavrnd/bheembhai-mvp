#!/usr/bin/env bash
# Runs one skill and ALWAYS publishes a classified result.json (see DESIGN-remote-results.md).
set -uo pipefail
# NOTE: bb_step_result.json, not result.json — the PDLC skills use result.json as their own
# in-repo handoff artifact, so the control plane keeps a separate, unambiguous name.
RESULT="${RESULT_DIR:-/out}/bb_step_result.json"
# Workspace dir — image-owned and container-local (ADR-014: zero mounts), overridable
# so the script can also run on the host (tests/unit/agent/test_run_skill_reentry.py
# points it at a temp dir).
WORKSPACE="${WORKSPACE_DIR:-/workspace}"
# Per-attempt upload channels (ADR-014): the engine presigns one PUT URL per channel.
# Missing vars = "host test mode" — the upload helpers skip silently, never fail.
AGENT_LOG="${RESULT_DIR:-/out}/agent.log"
DIAG="${RESULT_DIR:-/out}/diagnostics.txt"
START=$(date +%s)
emit () {  # status, reason, [next]
  # Build the JSON with ONE jq call from environment variables. The previous version
  # interpolated several $(...) substitutions inside a heredoc: if any of them failed
  # (odd characters in a multi-KB agent summary), the file was left truncated and
  # unparseable — the engine then saw "no result" and classified the step
  # failed_incomplete even though the work had succeeded.
  BB_STATUS="$1" BB_REASON="${2:-}" BB_NEXT="${3:-}" \
  BB_RUN="${RUN_ID:-}" BB_STEP="${STEP_ID:-}" BB_ATTEMPT="${ATTEMPT_NO:-1}" \
  BB_SKILL="${SKILL:-}" BB_SUMMARY="${SUMMARY:-}" BB_SUMMARY_FULL="${SUMMARY_FULL:-}" \
  BB_ARTIFACT="${ARTIFACT:-}" \
  BB_COST="${COST_USD:-0}" BB_COST_REPORTED="${COST_REPORTED:-0}" BB_DUR="$(( $(date +%s) - START ))" \
  BB_COMMIT="${COMMIT_SHA:-}" BB_MODELS="${MODELS_USED:-}" BB_MODEL_REQ="${BB_MODEL:-}" \
  jq -n --argjson files "${FILES_JSON:-[]}" --argjson review "${REVIEW_JSON:-[]}" '{
        run_id: env.BB_RUN, step_id: env.BB_STEP,
        attempt_no: (env.BB_ATTEMPT | tonumber? // 1),
        skill: env.BB_SKILL, status: env.BB_STATUS,
        reason: env.BB_REASON,
        next: (if env.BB_NEXT == "" then null else env.BB_NEXT end),
        summary: env.BB_SUMMARY, summary_full: env.BB_SUMMARY_FULL,
        artifact: env.BB_ARTIFACT,
        files: $files, review_files: $review, commit: env.BB_COMMIT,
        models_used: env.BB_MODELS, model_requested: env.BB_MODEL_REQ,
        cost_usd: (env.BB_COST | tonumber? // 0),
        cost_reported: ((env.BB_COST_REPORTED // "0") == "1"),
        duration_s: (env.BB_DUR | tonumber? // 0)
      }' > "$RESULT".tmp 2>"$RESULT".err

  # Only move into place if it actually parsed — never leave a half-written result.
  if [ -s "$RESULT".tmp ] && jq -e . "$RESULT".tmp >/dev/null 2>&1; then
    mv "$RESULT".tmp "$RESULT"
    rm -f "$RESULT".err
  else
    # last-ditch minimal result so the engine always gets a verdict it can read.
    # But first log WHY the rich emit failed — a silent fallback hides the real bug
    # (e.g. an invalid --argjson from a corrupted FILES_JSON / REVIEW_JSON).
    { echo "WARN: rich result emit failed — wrote minimal fallback"
      echo "  jq stderr: $(head -c 200 "$RESULT".err 2>/dev/null)"
      echo "  files_json: ${FILES_JSON:-}"
      echo "  review_json: ${REVIEW_JSON:-}"; } >> "$DIAG" 2>/dev/null || true
    printf '{"run_id":"%s","step_id":"%s","attempt_no":%s,"status":"%s","summary":"(summary unavailable)","files":[],"cost_usd":%s}\n' \
      "${RUN_ID:-}" "${STEP_ID:-}" "${ATTEMPT_NO:-1}" "$1" "${COST_USD:-0}" > "$RESULT"
    rm -f "$RESULT".tmp "$RESULT".err
  fi
}

fail () { emit "$1" "$2"; exit "${3:-1}"; }

# --- progress heartbeat -------------------------------------------------------
# The engine polls this file from Object Storage (ADR-014), so a long-running stage
# shows liveness instead of silence. Uploads are presigned PUTs, best-effort.
PROGRESS="${RESULT_DIR:-/out}/progress.json"
progress () {  # phase, note
  printf '{"phase":"%s","note":"%s","elapsed_s":%d,"ts":%d}\n' \
    "$1" "${2:-}" "$(( $(date +%s) - START ))" "$(date +%s)" > "$PROGRESS" 2>/dev/null || true
}
heartbeat_loop () {   # keeps elapsed_s ticking while the agent works
  while true; do
    sleep 5
    progress "${CURRENT_PHASE:-working}" "${CURRENT_NOTE:-agent running}"
    # Phase 2: re-upload the heartbeat channels every tick so the engine sees
    # liveness and a fresh agent.log (≤5s staleness — fine for the cost scrape).
    upload_try "${BB_PROGRESS_PUT_URL:-}" "$PROGRESS" application/json || true
    upload_try "${BB_LOG_PUT_URL:-}" "$AGENT_LOG" text/plain || true
    # also print, so `docker logs` shows the container is alive even while the
    # agent itself is quiet (claude -p buffers until it finishes).
    echo "[$(( $(date +%s) - START ))s] ${CURRENT_PHASE:-working}: ${CURRENT_NOTE:-agent running}"
  done
}

# --- Object Storage upload helpers (ADR-014: presigned PUTs) -------------------
# The engine presigns one PUT URL per channel; the container holds no cloud
# credentials. upload_try is best-effort (auxiliary channels); upload_critical
# carries the result payload, whose PUT failure degrades the exit code to 4
# (failed_infra — the engine retries; the push already landed, so a retry is
# idempotent). Both no-op on an unset URL or a missing/empty file, so host-run
# tests and re-entry hooks behave exactly as before. `${VAR:-}` guards keep them
# safe under `set -u` — including from the EXIT trap, where an unbound-variable
# abort would silently kill the whole upload path. URLs are bearer credentials
# and are never echoed.
upload_try () {  # put_url, local_file, content_type
  [ -n "${1:-}" ] && [ -s "${2:-}" ] || return 0
  curl -fsS -X PUT --data-binary @"$2" -H "Content-Type: $3" "$1" >/dev/null 2>&1
}
upload_critical () {  # put_url, local_file, content_type
  [ -n "${1:-}" ] && [ -s "${2:-}" ] || return 0
  curl -fsS --retry 3 --retry-delay 2 --connect-timeout 15 \
    -X PUT --data-binary @"$2" -H "Content-Type: $3" "$1" >/dev/null 2>&1
}
on_exit () {
  _rc=$?
  # Abnormal exits (e.g. a `set -u` abort) skip the normal kill paths — reap the
  # heartbeat here so it never orphans holding the log/stdout descriptors open.
  # Guarded: HB_PID may legitimately be unset if the script exits before line
  # 555 starts the loop.
  kill "${HB_PID:-}" 2>/dev/null || true
  # The result payload is the ONE critical channel — no PUT, no verdict.
  if ! upload_critical "${BB_RESULT_PUT_URL:-}" "$RESULT" application/json; then
    _rc=4
    # Leave a local verdict matching the exit code: engine-side this classifies
    # as failed_infra even if a later retry's PUT succeeds.
    if [ -s "$RESULT" ]; then
      jq --arg reason "result upload failed (presigned PUT) — engine will retry the step" \
         '.status = "failed_infra" | .reason = $reason' "$RESULT" > "$RESULT".tmp \
        && mv "$RESULT".tmp "$RESULT" 2>/dev/null
    else
      printf '{"status":"failed_infra","reason":"result upload failed (presigned PUT) — engine will retry the step"}\n' > "$RESULT"
    fi
  fi
  # Auxiliary channels — the final progress heartbeat and the logs ride along
  # so the engine sees the last state even though the heartbeat loop is dead.
  upload_try "${BB_PROGRESS_PUT_URL:-}" "$PROGRESS" application/json || true
  upload_try "${BB_LOG_PUT_URL:-}" "$AGENT_LOG" text/plain || true
  upload_try "${BB_DIAG_PUT_URL:-}" "$DIAG" text/plain || true
  exit "$_rc"
}
trap on_exit EXIT
if [ -z "${BB_RESULT_PUT_URL:-}" ] && [ -z "${BB_PROGRESS_PUT_URL:-}" ] \
   && [ -z "${BB_LOG_PUT_URL:-}" ] && [ -z "${BB_DIAG_PUT_URL:-}" ]; then
  echo "WARNING: no BB_*_PUT_URL provided — running WITHOUT S3 uploads (host test mode)" >&2
fi
progress init "starting up"
[ -n "${SKILL:-}" ] || fail failed_init "no SKILL provided" 2
# Zero mounts (ADR-014): $WORKSPACE and the result dir are image-owned and
# container-local — create them here (they must exist even in host-run tests).
# Everything below writes to them; a non-writable dir makes the git clone die with
# a confusing "could not create work tree dir" and the result file can't be
# published either — so fail loudly on stderr (survives in docker logs + the
# engine's log capture) instead of exiting 3 with no trace.
mkdir -p "$WORKSPACE" "${RESULT_DIR:-/out}"
[ -w "${RESULT_DIR:-/out}" ] || {
  echo "FATAL: ${RESULT_DIR:-/out} is not writable by $(id -un)" >&2
  fail failed_init "result dir ${RESULT_DIR:-/out} not writable" 2
}
[ -w "$WORKSPACE" ] || {
  echo "FATAL: $WORKSPACE is not writable by $(id -un)" >&2
  fail failed_init "$WORKSPACE not writable" 2
}

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
  cd "$WORKSPACE"
  # Re-entry guard: a previous visit of this step (routing re-loop, gate send-back, or a
  # crash-recovery relaunch of the same attempt) reuses this workspace dir with its clone
  # still in place. `git clone` into a non-empty dir fails instantly ("destination path
  # 'repo' already exists"), so drop the leftover first — the clone below then resumes
  # from the last pushed state, which is exactly what push-lands-or-retry wants.
  [ -d "$WORKSPACE/repo" ] && rm -rf "$WORKSPACE/repo"
  # Try the run branch first (steps 2+); if it doesn't exist yet, create it from source.
  if git clone --quiet --single-branch --branch "$RUN_BRANCH" "$AUTH_URL" repo 2>/dev/null; then
    echo "cloned existing run branch $RUN_BRANCH" | tee -a "$AGENT_LOG"
    NEW_BRANCH=0
  else
    SRC="${GIT_SOURCE_BRANCH:-main}"
    git clone --quiet --single-branch --branch "$SRC" "$AUTH_URL" repo \
      || fail failed_init "could not clone $GIT_REMOTE_URL @ $SRC" 3
    cd repo && git checkout -q -b "$RUN_BRANCH" && cd ..
    echo "created run branch $RUN_BRANCH from $SRC" | tee -a "$AGENT_LOG"
    NEW_BRANCH=1
  fi
  # scrub credentials from the remote so the token never sits in .git/config
  ( cd repo && git remote set-url origin "$GIT_REMOTE_URL" 2>/dev/null || true )
  # everything below expects the repo at $WORKSPACE; point at the clone
  ln -sfn "$WORKSPACE/repo" "$WORKSPACE/_repo" 2>/dev/null || true
  WORKDIR_REPO="$WORKSPACE/repo"
else
  WORKDIR_REPO="$WORKSPACE"
fi

# Test hook: stop after git init — lets the re-entry regression test
# (tests/unit/agent/test_run_skill_reentry.py) run the real script end-to-end
# against a local repo without launching Claude Code.
[ "${BB_STOP_AFTER_INIT:-0}" = "1" ] && exit 0

# --- INIT: deliver the step's ONE skill into .claude/skills (Phase 1: S3) ---
# The image is a pure runtime — no skills baked in. The engine pins this step's
# skill bundle at init and signs a fresh presigned GET per launch, passed as
# BB_SKILL_URL + BB_SKILL_SHA256. BheemBhai is authoritative: the download
# OVERWRITES whatever .claude/skills the repo tracks (the COMMIT block restores
# tracked .claude before staging so the bundle never lands on the branch).
[ -n "${BB_SKILL_URL:-}" ] || fail failed_init "no BB_SKILL_URL (skill bundle) provided" 2
progress skills "downloading skill bundle"
mkdir -p "${WORKDIR_REPO}/.claude"
rm -rf "${WORKDIR_REPO}/.claude/skills"   # drops repo-tracked + stale symlinks alike
mkdir -p "${WORKDIR_REPO}/.claude/skills"
TARBALL="/tmp/bb-skill-${SKILL}.tar.gz"
# -f: a 403 (expired presign) fails rather than writing the XML error body;
# --retry covers transient network drops — the engine retries the rest.
curl -fsSL --retry 2 --connect-timeout 15 "$BB_SKILL_URL" -o "$TARBALL" \
  || fail failed_infra "skill bundle download failed for ${SKILL} (curl rc=$?)" 4
if [ -n "${BB_SKILL_SHA256:-}" ]; then
  # sha256sum -c with a stdin manifest (nb: no -s flag — busybox's sha256sum
  # rejects it); non-zero exit = mismatch.
  printf '%s  %s\n' "$BB_SKILL_SHA256" "$TARBALL" | sha256sum -c - >/dev/null 2>&1 \
    || fail failed_infra "skill bundle sha256 mismatch for ${SKILL} — refusing to extract" 4
fi
# Untrusted archive: refuse any entry that escapes the skill dir (absolute path
# or a `..` component) before extracting. grep exit 1 = clean listing.
if tar -tzf "$TARBALL" 2>/dev/null | grep -qE '(^|/)\.\.(/|$)|^/'; then
  fail failed_infra "skill bundle for ${SKILL} contains unsafe paths (absolute or ..)" 4
fi
tar -xzf "$TARBALL" -C "${WORKDIR_REPO}/.claude/skills" \
  || fail failed_infra "skill bundle extract failed for ${SKILL}" 4
rm -f "$TARBALL"
progress skills "skill delivered: ${SKILL}"

# Test hook: stop after the skills overlay — lets the overlay regression test
# (tests/unit/agent/test_run_skill_reentry.py) run the real script against a
# local repo without launching Claude Code.
[ "${BB_STOP_AFTER_SKILLS:-0}" = "1" ] && exit 0

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
# The container is destroyed after the run, so anything worth inspecting must be
# written to the container-local /out now; the EXIT trap uploads it via the
# presigned BB_DIAG_PUT_URL. This block is what tells you WHY an MCP call was denied.
{
  echo "=== identity ==="
  echo "uid=$(id -u) user=$(id -un) HOME=${HOME:-unset}"
  echo "perm_flags_will_be: $( [ "$(id -u)" -ne 0 ] && echo '--dangerously-skip-permissions' || echo '--permission-mode acceptEdits (ROOT FALLBACK)' )"
  echo
  echo "=== credentials present (values redacted) ==="
  for v in JIRA_URL JIRA_USERNAME JIRA_API_TOKEN GH_TOKEN ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN; do
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
  echo "$WORKSPACE writable: $( [ -w "$WORKSPACE" ] && echo YES || echo NO )"
  echo "/out writable:       $( [ -w "${RESULT_DIR:-/out}" ] && echo YES || echo NO )"
  echo
  echo "=== skill bundle (S3) ==="
  # Presigned URL: print the HOST only — the query string is a bearer credential.
  if [ -n "${BB_SKILL_URL:-}" ]; then
    echo "  BB_SKILL_URL: SET (host=$(printf '%s' "$BB_SKILL_URL" | sed -E 's#^([a-z]+://[^/]+).*#\1#'))"
  else
    echo "  BB_SKILL_URL: MISSING"
  fi
  echo "  BB_SKILL_SHA256: $([ -n "${BB_SKILL_SHA256:-}" ] && echo SET || echo MISSING)"
} > "$DIAG" 2>&1

{
  echo
  echo "=== MCP config ==="
  echo "status: ${MCP_STATUS:-unknown}"
  for f in "${HOME:-/home/node}/bb-mcp.json" "$WORKSPACE/.mcp.json"; do
    if [ -f "$f" ]; then
      echo "  $f: present, placeholders_remaining=$(grep -c '\${' "$f" 2>/dev/null || echo 0)"
      echo "    servers: $(jq -r '(.mcpServers // {}) | keys | join(",")' "$f" 2>/dev/null || echo unparseable)"
      echo "    jira_url: $(jq -r '.mcpServers.atlassian.env.JIRA_URL // "MISSING"' "$f" 2>/dev/null)"
    else
      echo "  $f: ABSENT"
    fi
  done
} >> "$DIAG" 2>&1

# --- CONTEXT: materialize the engine's BB_CONTEXT env to the file the digest
# below reads. Phase 1 dropped the /ctx bind mount — the context travels in the
# env and the runner writes it here (under $HOME, ours alone by construction).
if [ -n "${BB_CONTEXT:-}" ]; then
  if [ -n "${CONTEXT_FILE:-}" ]; then
    printf '%s' "$BB_CONTEXT" > "$CONTEXT_FILE" 2>/dev/null \
      || echo "WARNING: could not write context to ${CONTEXT_FILE} — step runs without gate/reviewer context" >&2
  else
    echo "WARNING: BB_CONTEXT set but CONTEXT_FILE unset — context ignored" >&2
  fi
fi

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
  # Ad-hoc sessions (BB_MODE=adhoc): the user's free-form query rides the same
  # context channel as everything else and becomes the prompt verbatim.
  USER_QUERY=$(jq -r '.user_query // ""' "$CONTEXT_FILE" 2>/dev/null || echo "")
fi

# --- DEMO/MOCK MODE ---
if [ "${BB_MOCK:-0}" = "1" ]; then
  sleep "${BB_MOCK_SECONDS:-4}"
  case "${BB_MOCK_FORCE:-}" in
    crash) exit 137 ;;
    fail)  fail failed_execution "mock deterministic failure" 1 ;;
    block) SUMMARY="Tests are not honestly green." COST_USD=0.02 COST_REPORTED=1 emit BLOCK "mock block" implement; exit 0 ;;
  esac
  ST="${STORY_ID:+ for $STORY_ID}"
  if [ "$GATE_FOLLOWS" = "true" ]; then
    SUMMARY="${SKILL} finished (mock)${ST}. Written for a reviewer: ready to approve."
  else
    SUMMARY="${SKILL} finished (mock)${ST}."
  fi
  ARTIFACT="docs/${SKILL}.md" COST_USD=0.0${RANDOM:0:2} COST_REPORTED=1
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

# --- AD-HOC MODE (ADR-016) -------------------------------------------------
# The user's query IS the prompt. No BB_OUTCOME/BB_REVIEW vocabulary (the
# verdict defaults to completed when absent) — the agent's final reply is
# shown to the user verbatim and becomes the summary. Everything else (clone,
# MCP, diagnostics, commit/push, upload channels) is identical to workflow mode.
if [ "${BB_MODE:-workflow}" = "adhoc" ]; then
  [ -n "${USER_QUERY:-}" ] || fail failed_execution "BB_MODE=adhoc but the context carries no user_query" 1
  PROMPT="You are an ad-hoc agent session working on the user's branch (${RUN_BRANCH:-unknown}). Follow the house style in ${WORKDIR_REPO}/.claude/skills/${SKILL}/SKILL.md — it describes how to operate in this session.

The user asks:
---
${USER_QUERY}
---

Do exactly what the user asked, in the repo's working tree. Verify your work (run tests or builds where relevant) before you finish. The session runner commits and pushes your changes automatically when you finish — never run git commit, git push, or create branches yourself.

When you are completely finished, end your reply with a concise report for the user: what you changed (file paths), what you verified, and anything they should know (assumptions, follow-ups, risks). Your final message is shown to the user verbatim — write it for them; no outcome codes, no protocol lines."
  # The transcript gets a marker that the query is loaded and the run is about
  # to start — also the re-entry harness's assertion point (it stops the run
  # right after prompt composition, before Claude Code is invoked).
  echo "adhoc mode: prompt ready (${#PROMPT} bytes) — query head: $(printf '%s' "$USER_QUERY" | head -c 100)" >> "$AGENT_LOG" 2>/dev/null || true
fi

# Test/verification hook: stop right after the prompt is composed — the unit
# harness asserts the prompt contract without invoking Claude Code.
[ "${BB_STOP_AFTER_PROMPT:-0}" = "1" ] && exit 0

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
  echo "WARNING: running as root — Claude Code blocks --dangerously-skip-permissions." | tee -a "$AGENT_LOG"
  echo "         Bash/MCP tool calls will be denied. Rebuild the image so it runs as non-root." | tee -a "$AGENT_LOG"
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
echo "MODEL_FLAG=${MODEL_FLAG:-<none>}" >> "$DIAG" 2>/dev/null || true
echo "MODEL_PROFILE=${BB_ACTIVE_PROFILE:-anthropic} (resolved model: ${BB_MODEL:-default})" \
  >> "$DIAG" 2>/dev/null || true
if [ -n "${ANTHROPIC_BASE_URL:-}" ]; then
  echo "ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL} (vendor endpoint; needs ANTHROPIC_AUTH_TOKEN)" \
    >> "$DIAG" 2>/dev/null || true
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
} >> "$DIAG" 2>&1 || true

CURRENT_PHASE=agent
CURRENT_NOTE="claude is working${STORY_ID:+ on $STORY_ID}"
progress agent "$CURRENT_NOTE"
heartbeat_loop & HB_PID=$!

# Stream the agent's output to the container log AND capture it, so `docker logs -f` shows
# progress live instead of the run being a black box. (Previously $(...) swallowed everything.)
LOGFILE="$AGENT_LOG"
echo "=== running ${SKILL}${STORY_ID:+ for $STORY_ID} ===" | tee -a "$LOGFILE"
set -o pipefail
cd "${WORKDIR_REPO:-/workspace}"

# --- COMMIT (shared) ----------------------------------------------------------
# Commits + pushes the working tree and records WHICH files were touched, so a
# reviewer at a gate can open the actual artifacts (story-design.md, test-plan.md,
# ...) rather than only reading the summary. A function, not inline code, because
# session mode (ADR-016) runs the SAME commit+push after every turn — the
# plumbing filters must behave identically. Defined BEFORE the session block
# (bash resolves functions at call time).
commit_and_push () {
  FILES_JSON="[]"
  COMMIT_SHA=""
  REPO="${WORKDIR_REPO:-/workspace}"
  if [ -d "${REPO}/.git" ]; then
    cd "$REPO"
    # BheemBhai-authoritative skills: the downloaded bundle replaced whatever
    # .claude/skills the repo tracked, and that content must never land on the
    # branch — drop the download and restore the repo's own .claude before
    # staging (repo-tracked skills come back unmodified, so no spurious diffs),
    # then let the plumbing filter below run as usual.
    rm -rf .claude/skills 2>/dev/null || true
    git restore .claude 2>/dev/null || true
    # Never commit platform plumbing into the user's branch: the skills symlink and our MCP
    # config live in the working tree but must not land on their history. If the repo
    # TRACKS .claude it is the repo's OWN content (some projects version their skills) —
    # leave it alone; the filter targets only the plumbing our agent injects.
    if git ls-files --error-unmatch .claude >/dev/null 2>&1; then
      echo "repo tracks .claude — left intact (not platform plumbing)" \
          >> "$DIAG" 2>/dev/null || true
    else
      git rm -r --cached --quiet .claude 2>/dev/null || true
      rm -f .mcp.json 2>/dev/null || true
      # Idempotent: session mode runs this per turn, and an unconditional
      # append would re-diff .gitignore every call → a spurious commit per
      # turn (observed: 4 commits for a 2-turn session).
      grep -qxF '.claude/' .gitignore 2>/dev/null || printf '.claude/\n' >> .gitignore
      grep -qxF '.mcp.json' .gitignore 2>/dev/null || printf '.mcp.json\n' >> .gitignore
    fi
    git add -A 2>/dev/null
    # NB: no `|| echo "[]"` fallback here — under `set -o pipefail` a grep with zero matches
    # makes the whole pipeline exit 1 even when jq succeeded, so the fallback echo would
    # APPEND a second "[]" to jq's own output ("[]\n[]") and corrupt the JSON (emit's
    # --argjson then fails downstream). jq -Rn prints `[]` by itself on empty input; the
    # guard below covers the only remaining failure mode (a jq parse error).
    FILES_JSON=$(git diff --cached --name-status 2>/dev/null | \
      grep -v -E '^[A-Z][[:space:]]+\.claude/' | \
      grep -v -E '^[A-Z][[:space:]]+\.mcp\.json$' | \
      grep -v -E '^[A-Z][[:space:]]+\.gitignore$' | \
      jq -Rn '[inputs | split("\t") | select(length >= 2) |
               {status: .[0], path: .[-1]}]' 2>/dev/null)
    [ -n "$FILES_JSON" ] || FILES_JSON="[]"
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
      if git push --quiet "$PUSH_URL" "HEAD:${RUN_BRANCH}" 2>>"$AGENT_LOG"; then
        echo "pushed to ${RUN_BRANCH} (${COMMIT_SHA})" | tee -a "$AGENT_LOG"
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
        sed "s|^${REPO}/||" | jq -Rn '[inputs | {status:"M", path:.}]' 2>/dev/null)
    [ -n "$FILES_JSON" ] || FILES_JSON="[]"
  fi
}

# --- SESSION MODE (ADR-016 Phase 2) ------------------------------------------
# Multi-turn live container: claude stays alive as a coprocess speaking
# stream-json on stdin/stdout. The engine drops each turn into an Object-Storage
# inbox (one stable presigned GET key, overwritten per turn, monotonic seq) and
# the container publishes its per-turn reply to the outbox (presigned PUT) after
# committing + pushing via the SAME commit_and_push as the single-shot path. An
# `end` sentinel commits + pushes once more and exits cleanly — the engine waits
# for this exit and never falls through to the single-shot invocation below.
if [ "${BB_SESSION:-0}" = "1" ]; then
  [ -n "${BB_INBOX_GET_URL:-}" ] || fail failed_init "session mode requires BB_INBOX_GET_URL" 2
  [ -n "${BB_OUTBOX_PUT_URL:-}" ] || fail failed_init "session mode requires BB_OUTBOX_PUT_URL" 2
  [ -n "${BB_SESSION_ID:-}" ] || fail failed_init "session mode requires BB_SESSION_ID" 2
  CLAUDE_HELP=$(claude --help 2>&1 || true)
  printf '%s' "$CLAUDE_HELP" | grep -q -- "--input-format" \
    || fail failed_infra "session mode requires claude --input-format (stream-json) — upgrade the agent image" 4
  # ── Session identity + resume (ADR-016 Phase 3) ─────────────────────────
  # One engine-minted session id per run, passed to every incarnation. The
  # transcript path is derived from it + the CWD claude inherits (the CLI
  # munges / to - under ~/.claude/projects/<munged>), so a cold-start restore
  # lands the file exactly where --resume looks for it. The slash after
  # projects/ is load-bearing: the munged PWD starts with a leading dash
  # (the leading / munged), so concatenating without it collapses
  # projects/ + -workspace-repo into projects-workspace-repo.
  TRANSCRIPT_DIR="$HOME/.claude/projects/$(printf '%s' "$PWD" | tr '/' '-')"
  TRANSCRIPT_FILE="${TRANSCRIPT_DIR}/${BB_SESSION_ID}.jsonl"
  SESSION_FLAGS="--session-id ${BB_SESSION_ID}"
  if [ "${BB_SESSION_RESUME:-0}" = "1" ]; then
    if [ -n "${BB_TRANSCRIPT_GET_URL:-}" ]; then
      mkdir -p "$TRANSCRIPT_DIR"
      curl -fsS --connect-timeout 15 --max-time 60 "$BB_TRANSCRIPT_GET_URL" \
        -o "$TRANSCRIPT_FILE" 2>/dev/null || true
    fi
    # --resume by id searches only the current project dir — exactly the path
    # restored above. No transcript (the first incarnation died before claude
    # saved one) → fall back to a fresh --session-id session.
    if [ -s "$TRANSCRIPT_FILE" ] && printf '%s' "$CLAUDE_HELP" | grep -q -- "--resume"; then
      SESSION_FLAGS="--resume ${BB_SESSION_ID}"
      # The system prompt embeds cwd + git status; excluding them keeps the
      # resumed context comparable across containers.
      printf '%s' "$CLAUDE_HELP" | grep -q -- "--exclude-dynamic-system-prompt-sections" \
        && SESSION_FLAGS="${SESSION_FLAGS} --exclude-dynamic-system-prompt-sections"
      echo "[session] resuming ${BB_SESSION_ID} (transcript restored, $(wc -c < "$TRANSCRIPT_FILE") bytes)" >> "$LOGFILE"
    else
      echo "[session] resume requested but no transcript to restore — starting fresh with --session-id" >> "$LOGFILE"
    fi
  fi
  echo "=== session started (stream-json coprocess) ===" | tee -a "$LOGFILE"

  # The preamble is the session's standing instruction — the ad-hoc house style
  # WITHOUT any turn query. Turn 1 arrives via the inbox exactly like every
  # later turn, so a cold-start re-delivery of the same seq is one uniform path.
  SESSION_PROMPT="You are an ad-hoc agent session working on the user's branch (${RUN_BRANCH:-unknown}). Follow the house style in ${WORKDIR_REPO}/.claude/skills/${SKILL}/SKILL.md — it describes how to operate in this session.

You are in an interactive session: the user sends one message at a time, and your reply to each is shown to them verbatim. Do exactly what the user asked, in the repo's working tree. Verify your work (run tests or builds where relevant) before you reply. The session runner commits and pushes your changes automatically after each reply — never run git commit, git push, or create branches yourself.

When you finish each task, end your reply with a concise report for the user: what you changed (file paths), what you verified, and anything they should know (assumptions, follow-ups, risks). Write replies for the user directly — no outcome codes, no protocol lines."

  # Start claude as a coprocess: stdout carries stream-json events (read below),
  # stderr goes straight to the transcript log. `exec` makes the coproc subshell
  # IS claude, so CLAUDE_PID signals reach the CLI itself (SIGINT = graceful
  # session save — Phase 3 resumes from it).
  coproc CLAUDE { exec claude $PERM_FLAGS $MODEL_FLAG $MCP_FLAG $SESSION_FLAGS \
      --input-format stream-json --output-format stream-json --verbose \
      2>>"$LOGFILE"; }
  # bash UNSETS $CLAUDE_PID once it reaps the coproc's exit — under `set -u` any
  # later $CLAUDE_PID use (kill -0 liveness, SIGINT, wait) would abort the script
  # mid-session (observed: "CLAUDE_PID: unbound variable" on idle death, rc 1
  # before the result emit). Keep our own copy; it never goes away.
  CLAUDE_ALIVE_PID="${CLAUDE_PID:-}"

  # Send one user message to the coprocess stdin. The content is a content-block
  # array (the same shape the CLI emits), so arbitrary text survives untouched.
  claude_send () {  # message
    jq -nc --arg m "$1" \
      '{type:"user",message:{role:"user",content:[{type:"text",text:$m}]}}' \
      >&"${CLAUDE[1]}" 2>/dev/null || true
  }

  # Read the coprocess stdout until the terminal result event for the current
  # turn. Every line lands in the transcript log (the rendered-view parser reads
  # this file). Sets LAST_RESULT_FILE and returns 0 on success; returns 1 on EOF
  # with no result (claude died mid-turn).
  await_result () {
    LAST_RESULT_FILE=""
    local line typ
    while IFS= read -r line; do
      printf '%s\n' "$line" >> "$LOGFILE"
      typ=$(jq -r '.type // empty' <<<"$line" 2>/dev/null)
      [ "$typ" = "result" ] || continue
      LAST_RESULT_FILE="${RESULT_DIR:-/out}/last_result.jsonl"
      printf '%s\n' "$line" > "$LAST_RESULT_FILE"
      return 0
    done <&"${CLAUDE[0]}"
    return 1
  }

  # Cost + model bookkeeping per turn. total_cost_usd on a result event is the
  # SESSION's cumulative spend, so the turn's cost is the delta; if the value is
  # not monotonic (per-request accounting in some CLI builds) fall back to raw.
  # TURN_COST feeds the outbox; COST_USD always carries the accumulated total so
  # every later emit/fail path reports the session's real spend.
  extract_turn_cost () {
    local total
    total=$(jq -r '.total_cost_usd // empty' "$LAST_RESULT_FILE" 2>/dev/null)
    TURN_COST="0"; COST_REPORTED=0
    case "$total" in ''|*[!0-9.]*) return 0 ;; esac
    COST_REPORTED=1
    if awk -v t="$total" -v s="$SESSION_COST_TOTAL" 'BEGIN{exit !(t >= s)}'; then
      TURN_COST=$(awk -v t="$total" -v s="$SESSION_COST_TOTAL" 'BEGIN{printf "%.6f", t - s}')
    else
      TURN_COST="$total"
    fi
    SESSION_COST_TOTAL="$total"
    COST_USD="$SESSION_COST_TOTAL"
    MODELS_USED=$(jq -r '(.modelUsage // {}) | keys | join(",")' "$LAST_RESULT_FILE" 2>/dev/null)
    [ -n "$MODELS_USED" ] && echo "MODELS_USED=$MODELS_USED (requested: ${BB_MODEL:-default})" \
        >> "$DIAG" 2>/dev/null || true
  }

  # One turn: send the query, wait for the result, commit+push, publish outbox.
  # Every failure path exits the script via session_fail — the engine sees a
  # dead container and re-delivers the turn on a fresh one (the push already
  # landed, so the redo starts from the last pushed state).
  run_turn () {  # seq, query
    local seq="$1" query="$2" resp subtype
    CURRENT_PHASE=answering
    CURRENT_NOTE="answering turn ${seq}"
    progress answering "$CURRENT_NOTE"
    echo "[session] turn ${seq}: query $(printf '%s' "$query" | head -c 80)" >> "$LOGFILE"
    claude_send "$query"
    if ! await_result; then
      session_fail failed_execution "claude exited mid-turn ${seq} (EOF without a result event)" 1
    fi
    resp=$(jq -r '.result // ""' "$LAST_RESULT_FILE" 2>/dev/null)
    # is_error / error subtypes still carry a reply — deliver it to the user and
    # let the session continue (a failed request does not kill the process).
    subtype=$(jq -r '.subtype // ""' "$LAST_RESULT_FILE" 2>/dev/null)
    [ -n "$resp" ] || resp="(no result text — result subtype: ${subtype:-none})"
    extract_turn_cost
    # Never keep more than ~64 KB per turn in the control plane.
    RESP_FILE="${RESULT_DIR:-/out}/turn_response.txt"
    printf '%s' "$resp" > "$RESP_FILE"
    if [ "$(wc -c < "$RESP_FILE" 2>/dev/null || echo 0)" -gt 65536 ]; then
      head -c 65536 "$RESP_FILE" > "$RESP_FILE.tmp" && mv "$RESP_FILE.tmp" "$RESP_FILE"
    fi
    SUMMARY=$(head -c 1500 "$RESP_FILE" 2>/dev/null)
    SUMMARY_FULL=$(cat "$RESP_FILE" 2>/dev/null)
    commit_and_push
    progress publish "turn ${seq} answered — publishing outbox"
    # The outbox is the turn's terminal signal: the engine matches it by seq and
    # marks the turn complete WITHOUT killing the container. Its PUT failure
    # fails the turn (the engine re-delivers on a fresh container).
    OUTBOX="${RESULT_DIR:-/out}/outbox.json"
    BB_SEQ="$seq" BB_COST="${TURN_COST:-0}" BB_CREP="${COST_REPORTED:-0}" \
    BB_COMMIT="${COMMIT_SHA:-}" \
      jq -nc --argjson files "${FILES_JSON:-[]}" --rawfile response "$RESP_FILE" '{
          seq: (env.BB_SEQ | tonumber),
          response: $response,
          commit: (if env.BB_COMMIT == "" then null else env.BB_COMMIT end),
          files: $files,
          cost_usd: (env.BB_COST | tonumber? // 0),
          cost_reported: (env.BB_CREP == "1")
        }' > "$OUTBOX.tmp" 2>/dev/null
    [ -s "$OUTBOX.tmp" ] && mv "$OUTBOX.tmp" "$OUTBOX"
    if [ ! -s "$OUTBOX" ]; then
      session_fail failed_execution "outbox JSON build failed for turn ${seq}" 1
    fi
    upload_critical "${BB_OUTBOX_PUT_URL:-}" "$OUTBOX" application/json \
      || session_fail failed_infra "outbox upload failed (presigned PUT) — engine will retry the turn" 4
    echo "[session] turn ${seq}: answered (${#resp} chars, cost=${TURN_COST:-0}, files=$(printf '%s' "${FILES_JSON:-[]}" | jq length 2>/dev/null || echo '?'))" \
      | tee -a "$LOGFILE"
    CURRENT_PHASE=awaiting
    CURRENT_NOTE="turn ${seq} answered — awaiting next message"
    progress awaiting "$CURRENT_NOTE"
  }

  # Best-effort transcript upload (ADR-016 §3): the PUT is presigned at
  # launch, and a graceful exit ships the transcript so the next incarnation
  # can --resume. Never a failure — the turn/result channels already landed.
  upload_transcript () {
    [ -n "${BB_TRANSCRIPT_PUT_URL:-}" ] || return 0
    local tf="$TRANSCRIPT_FILE"
    # The derived path matches the CLI's munging (~/.claude/projects/<munged>);
    # if claude wrote elsewhere (a future CLI change), sweep ~/.claude instead.
    if [ ! -s "$tf" ]; then
      tf=$(find "$HOME/.claude" -name "${BB_SESSION_ID}.jsonl" -print -quit 2>/dev/null)
    fi
    [ -n "$tf" ] && upload_try "$BB_TRANSCRIPT_PUT_URL" "$tf" application/json || true
  }

  session_fail () {  # status, reason, exit_code
    kill -INT "${CLAUDE_ALIVE_PID:-0}" 2>/dev/null || true
    kill "$HB_PID" 2>/dev/null || true
    upload_transcript
    fail "$1" "$2" "${3:-1}"
  }

  # Explicit end (engine sentinel): SIGINT so claude saves its session
  # transcript, commit+push the final state, upload the transcript, publish
  # the result, exit 0 — the engine's _end_session waits for exactly this exit.
  session_end () {
    CURRENT_PHASE=ending
    CURRENT_NOTE="end sentinel — committing and closing"
    progress ending "$CURRENT_NOTE"
    kill -INT "${CLAUDE_ALIVE_PID:-0}" 2>/dev/null || true
    for _ in $(seq 1 10); do
      kill -0 "${CLAUDE_ALIVE_PID:-0}" 2>/dev/null || break
      sleep 0.5
    done
    commit_and_push
    kill "$HB_PID" 2>/dev/null || true
    upload_transcript
    COST_USD="${SESSION_COST_TOTAL:-0}"
    SUMMARY="session ended — ${TURNS_DONE:-0} turn(s), final state committed and pushed"
    progress publish "session ended"
    emit completed "session ended (explicit end)"; exit 0
  }

  session_main () {
    claude_send "$SESSION_PROMPT"
    # Consume the preamble's own reply (a result event) before the inbox loop —
    # otherwise turn 1's reader would stop at the PREAMBLE's result event.
    if ! await_result; then
      session_fail failed_execution "claude exited while reading the session preamble" 1
    fi
    # Seed the cumulative-cost baseline with the preamble's own spend: total_cost_usd
    # on result events is SESSION-CUMULATIVE, so without this turn 1's delta would
    # swallow the preamble's cost (observed 0.11 for a 0.10 turn).
    extract_turn_cost
    last_seen=0
    while true; do
      # Any fetch failure (404 before the first write, transient network) reads
      # as "no new turn" — the engine re-writes the same seq if it needs to.
      inbox=$(curl -fsS --connect-timeout 10 --max-time 20 "$BB_INBOX_GET_URL" 2>/dev/null || echo "")
      seq=$(printf '%s' "$inbox" | jq -r '.seq // empty' 2>/dev/null || echo "")
      if [ -z "$seq" ] || [ "$seq" = "$last_seen" ]; then
        if ! kill -0 "${CLAUDE_ALIVE_PID:-0}" 2>/dev/null; then
          wait "${CLAUDE_ALIVE_PID:-0}" 2>/dev/null; claude_rc=$?
          session_fail failed_execution "claude exited while awaiting input (rc=$claude_rc)" 1
        fi
        sleep 2
        continue
      fi
      last_seen="$seq"
      case "$(printf '%s' "$inbox" | jq -r '.kind // ""' 2>/dev/null)" in
        end)
          echo "[session] end sentinel (seq ${seq}) — closing" >> "$LOGFILE"
          session_end
          ;;
        turn)
          query=$(printf '%s' "$inbox" | jq -r '.query // ""' 2>/dev/null)
          [ -n "$query" ] || query="(empty query)"
          run_turn "$seq" "$query" || exit $?
          TURNS_DONE=$((TURNS_DONE + 1))
          ;;
        *)
          echo "[session] unknown inbox kind (seq ${seq}) — ignoring" >> "$LOGFILE"
          ;;
      esac
    done
  }

  SESSION_COST_TOTAL=0
  TURNS_DONE=0
  CURRENT_PHASE=awaiting
  CURRENT_NOTE="session up — awaiting first message"
  progress awaiting "$CURRENT_NOTE"
  session_main
  # session_main always ends in emit+exit (session_end / session_fail); a plain
  # return means a logic bug — never fall through into the single-shot path.
  session_fail failed_execution "session loop returned unexpectedly" 1
fi

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

# Extract session cost + models IMMEDIATELY — before any classifier can
# early-exit. Every emit (success OR fail) must carry what the session
# actually spent: the two real runs that burned $1.30 / $1.52 on deepseek
# recorded cost 0 because `fail` exits before the old late extraction.
# Anchored on the terminal result event (always the last stream-json line);
# modelUsage.*.costUSD is the fallback for a CLI version that drops the
# aggregate but keeps per-model accounting.
RC_COST=$(tail -n 50 "$LOGFILE" 2>/dev/null | grep '^{' | \
          jq -r 'select(.type=="result" and .total_cost_usd != null) | .total_cost_usd' 2>/dev/null | tail -1)
COST_REPORTED=0
case "$RC_COST" in ''|*[!0-9.]*) : ;; *) COST_USD="$RC_COST"; COST_REPORTED=1 ;; esac
if [ "$COST_REPORTED" = "0" ]; then
  RC_COST_SUM=$(tail -n 50 "$LOGFILE" 2>/dev/null | grep '^{' | \
                jq -r 'select(.type=="result") | .modelUsage | values | map(.costUSD) | add' 2>/dev/null | tail -1)
  case "$RC_COST_SUM" in ''|*[!0-9.]*) : ;; *) COST_USD="$RC_COST_SUM"; COST_REPORTED=1 ;; esac
fi
MODELS_USED=$(tail -n 50 "$LOGFILE" 2>/dev/null | grep '^{' | \
              jq -r 'select(.modelUsage != null) | .modelUsage | keys | join(",")' 2>/dev/null | tail -1)
[ -n "$MODELS_USED" ] && echo "MODELS_USED=$MODELS_USED (requested: ${BB_MODEL:-default})" \
    >> "$DIAG" 2>/dev/null || true

OUTPUT=$(tail -c 4000 "$LOGFILE" 2>/dev/null || echo "")

# Distinguish WHICH credential failed, so "the step failed" isn't a mystery. Model-auth
# (Anthropic/vendor key) is the most common and shows up as a 401; Jira/GitHub failures
# surface as MCP tool errors; git failures as push/clone errors (handled in the COMMIT block).
#
# These checks scan ONLY channels the CLI itself generates, never the whole transcript.
# Grepping the transcript raw is a false-positive minefield: every repo file and tool
# result the agent read lands there as one giant JSON line, and docs/code/ticket content
# routinely contains "api key", "401" and "invalid" text. Two real runs were misclassified
# failed_execution this way — "api key.*invalid" matched "SendGrid API key" ... "cache
# entries invalidated" 18 KB apart inside the repo's own architecture.md, and the Jira
# pattern matched a SUCCESSFUL atlassian tool result (the ticket's own JSON). The
# classifier exits BEFORE the COMMIT block, so in both cases the step's completed work
# was discarded uncommitted.
#
# Channels: (a) stream-json lines the CLI flagged "is_error":true — failed tool results
# and the error result payload; (b) the CLI's own stderr (non-JSON lines), consulted
# only when the run failed, where the CLI prints connection errors rather than content.
ERRLOG=$(grep -E '"is_error":true' "$LOGFILE" 2>/dev/null | tail -c 8000 || echo "")
STDERR_TEXT=""
if [ "$RC" -ne 0 ]; then
  STDERR_TEXT=$(grep -vE '^\{' "$LOGFILE" 2>/dev/null | tail -c 8000 || echo "")
  [ -z "$ERRLOG" ] && ERRLOG="$OUTPUT"   # non-streaming output: only the tail is available
fi
if [ -n "$ERRLOG" ]; then
  if printf '%s' "$ERRLOG" | grep -qiE '401 Authentication|authentication_failed|api[ _-]?key[^"]{0,80}invalid|invalid[^"]{0,80}api[ _-]?key'; then
    fail failed_execution "Model API rejected the credential (401). The ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN reaching the container is invalid, expired, or doesn't match ANTHROPIC_BASE_URL. This is NOT a Jira or git problem — it's the model credential." 1
  fi
  if printf '%s' "$ERRLOG" | grep -qiE 'mcp.*(unauthorized|401|invalid.token)|atlassian.*(401|forbidden)'; then
    fail failed_execution "Jira/Atlassian MCP authentication failed — check JIRA_API_TOKEN / JIRA_USERNAME. (Model auth was fine.)" 1
  fi
fi
if [ -n "$STDERR_TEXT" ] && printf '%s' "$STDERR_TEXT" | grep -qiE 'failed to connect to mcp server|mcp server .*(error|failed)'; then
  fail failed_execution "An MCP server failed to connect — check JIRA_API_TOKEN / JIRA_USERNAME / GH_TOKEN. (Model auth was fine.)" 1
fi

if [ "$RC" -ne 0 ]; then
  progress failed "agent exited $RC"
  fail failed_execution "agent exited non-zero ($RC): $(printf '%s' "$OUTPUT" | tail -c 400)" 1
fi
progress commit "agent finished, committing"

# --- COMMIT ---
# Same commit+push the session mode runs after every turn (defined above, before
# the session block) — one implementation, identical plumbing filters.
commit_and_push
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
#
# IMPORTANT: the agent's reply is MULTI-line text (the BB_OUTCOME / BB_REVIEW protocol
# lines live inside it). Never pipe jq's raw text output through `tail -1` — that keeps
# only the LAST line ("BB_OUTCOME: completed"), silently discarding the summary and every
# BB_REVIEW line. The result event is unique per stream, so no line-trimming is needed.
tail -n 50 "$LOGFILE" 2>/dev/null | grep '^{' | \
  jq -r 'select(.type=="result") | .result // empty' 2>/dev/null > "$SUMMARY_FILE" || true

if [ ! -s "$SUMMARY_FILE" ]; then
  tail -n 200 "$LOGFILE" 2>/dev/null | grep '^{' | \
    jq -r 'select(.type=="assistant") | .message.content[]? | select(.type=="text") | .text' \
    2>/dev/null | tail -c 4000 > "$SUMMARY_FILE" || true
fi
if [ ! -s "$SUMMARY_FILE" ]; then
  tail -c 1000 "$LOGFILE" > "$SUMMARY_FILE" 2>/dev/null || true
fi

# (session cost + models were extracted right after the CLI exited — see the
# EXECUTE block — so every fail path below already carries them in the emit)

# The agent reports its outcome as a BB_OUTCOME line in its reply (never as a file).
STATUS=$(grep -oE 'BB_OUTCOME:[[:space:]]*[A-Za-z_]+' "$SUMMARY_FILE" 2>/dev/null | tail -1 \
         | sed -E 's/.*BB_OUTCOME:[[:space:]]*//')
[ -n "$STATUS" ] || STATUS="completed"

# The skill declares which files a human should actually review via BB_REVIEW lines:
#   BB_REVIEW: path/to/file.py | short note on why
# We turn each into {path, note}. This is the curated list the UI shows by default instead
# of every git-touched file. If the skill emits none, review_files stays [] and the UI falls
# back to the full changed-file list — so nothing is ever hidden by omission.
# NB: same pipefail rule as FILES_JSON — no `|| echo "[]"` here, or a grep with zero
# matches would double jq's own `[]` output and corrupt the JSON.
REVIEW_JSON=$(grep -oE 'BB_REVIEW:[[:space:]]*.+' "$SUMMARY_FILE" 2>/dev/null | \
  sed -E 's/^BB_REVIEW:[[:space:]]*//' | \
  jq -Rn '[inputs
           | split("|")
           | {path: (.[0] | gsub("^[[:space:]]+|[[:space:]]+$";"")),
              note: (if length > 1 then (.[1] | gsub("^[[:space:]]+|[[:space:]]+$";"")) else "" end)}
           | select(.path != "")]' 2>/dev/null)
[ -n "$REVIEW_JSON" ] || REVIEW_JSON="[]"

# don't show the machine-readable lines to a human reviewer; cap the length.
# Take the HEAD of the reply — the agent opens with the actual summary (the
# BB_* protocol lines sit at the end), so tailing would start mid-sentence.
# The FULL stripped reply rides along as summary_full and lands in the DB, so
# a reviewer can load the complete summary on demand; the 1500-char head keeps
# poll payloads light. 64KB cap: env-var transport to jq (env.BB_SUMMARY_FULL)
# is safe at that size, far below the ~128KB per-var exec limit.
SUMMARY_FULL=$(sed -E '/BB_OUTCOME:[[:space:]]*[A-Za-z_]+/d; /BB_REVIEW:[[:space:]]*/d' \
               "$SUMMARY_FILE" 2>/dev/null | head -c 65536)
SUMMARY=$(printf '%s' "$SUMMARY_FULL" | head -c 1500)
progress publish "writing result"
emit "$STATUS" "ok"; exit 0
