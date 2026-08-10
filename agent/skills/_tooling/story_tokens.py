#!/usr/bin/env python3
"""
story_tokens.py — compute per-model token usage + estimated cost for one story-implement run,
from Claude Code session logs, for posting to Jira (customfield_10105 + a breakdown comment).

WHY PER-MODEL: the skills run different models per tier (opus for code-review/tech-design/
story-design, sonnet for implement, haiku for orchestration). Cost MUST be each model's tokens
times THAT model's rate — never a blended rate. This script groups usage by model and prices
each group separately, then sums to a total.

All three sources (OTEL / statusbar / JSONL) read the same Anthropic API usage.output_tokens,
which is the billing-accurate inclusive total — extended-thinking tokens are a SUBSET of
output_tokens, not counted separately. JSONL is billing-accurate, not an undercount.
Source differences are attribution scope (which turns are in scope), not thinking-token inclusion.

SOURCES (highest priority first):
  1. OTEL (~/.claude/otel-costs.json) — from claude_code.cost.usage metric via otel_receiver.py.
     Provides per-model cost directly from Claude Code's billing engine.
  2. statusbar JSON (--statusbar-json) — manual export from /usage; same underlying data.
  3. JSONL (auto via .claude/last-session.json or --session-id) — always present; reads
     usage.output_tokens which already INCLUDES extended-thinking tokens per the Anthropic
     API spec (thinking tokens are a subset of output_tokens, not counted separately).

ACCURACY NOTE: All three sources read from the same Anthropic API usage.output_tokens field,
which is the inclusive billing total. Thinking tokens are NOT excluded from JSONL.
The main accuracy difference between sources is attribution scope (which turns are in scope)
not thinking-token inclusion.

PRICING: per-model rates in USD per 1M tokens, with cache adjustments
  (cache_read ~0.1x input, cache_creation ~1.25x input). Edit RATES for your plan/models.

Usage:
  python story_tokens.py --story LNPRTL-30                           # auto (Stop hook)
  python story_tokens.py --story LNPRTL-30 --session-id <uuid>      # explicit override
  python story_tokens.py --story LNPRTL-30 --project-path /path     # project search
  python story_tokens.py --story LNPRTL-30 --statusbar-json sb.json # add statusbar total
  # output: JSON to stdout with total_cost_usd, total_tokens, per_model[],
  #         per_phase[], cost_drivers{}, source, caveat

Session isolation: auto-location ONLY works when called within the story's session (Step 6.5
of story-implement). Running outside an active session requires --session-id to avoid silently
attributing another session's tokens to this story.

Exit: 0 ok, 1 error.
"""

import argparse
import glob
import json
import os
import sys
from collections import defaultdict

# ---- Pricing (USD per 1,000,000 tokens) — JSONL FALLBACK ESTIMATE ONLY. ----
# Under "Option B", the cost written to Jira ALWAYS comes from Claude Code's billing engine
# via OTEL (claude_code.cost.usage) — see _load_otel_costs + build_report. These RATES are used
# ONLY to produce a rough ESTIMATE when the OTEL receiver wasn't running and all we have is JSONL
# token counts. Such an estimate is NEVER written to the cost field (customfield_10105) — it's
# labeled an estimate and the caller posts a comment instead. So this table can go stale (new
# models, price changes) without affecting the number that reaches Jira. Keep it only roughly
# current.
RATES = {
    "opus": {"input": 5.0, "output": 25.0},
    "sonnet": {"input": 3.0, "output": 15.0},
    "haiku": {"input": 1.0, "output": 5.0},
}
CACHE_READ_MULT = 0.10  # cache_read billed ~10% of input rate (estimate only)
CACHE_CREATE_MULT = 1.25  # cache_creation billed ~125% of input rate (estimate only)

# Phase-reset detection: a new compact phase starts when cache_read drops below this
# fraction of the running max seen in the current phase. 0.35 reliably catches the
# drop from 50K–100K → ~12K that happens after /compact, without false-positives from
# normal within-phase variation (cache reads grow monotonically within a phase).
_PHASE_RESET_RATIO = 0.35
_PHASE_RESET_MIN_MAX = 20_000  # only detect a reset when the phase grew past this size


def model_family(model_string):
    """Normalize 'claude-opus-4-8-2026...' -> 'opus'. Unknown -> the raw string."""
    if not model_string:
        return "unknown"
    s = model_string.lower()
    for fam in ("opus", "sonnet", "haiku"):
        if fam in s:
            return fam
    return s


def find_claude_root():
    return os.environ.get("CLAUDE_ROOT", os.path.expanduser("~/.claude"))


def encode_project_path(p):
    """Claude Code encodes the abs project path as the projects/ subdir name
    (commonly '/' -> '-'). We don't rely on the exact scheme; we glob+match instead."""
    return os.path.abspath(p)


def _read_last_session_file(base_dir):
    """Read .claude/last-session.json written by the Stop hook.

    Returns (session_id, transcript_path) or (None, None) if unavailable.
    """
    path = os.path.join(base_dir, ".claude", "last-session.json")
    if not os.path.exists(path):
        return None, None
    try:
        d = json.load(open(path))
        sid = d.get("session_id", "")
        tp = d.get("transcript_path", "")
        if sid and tp and os.path.exists(tp):
            return sid, tp
    except Exception:
        pass
    return None, None


def locate_session(session_id, project_path):
    # Priority 1: .claude/last-session.json written by the Stop hook (fastest, no searching).
    # Check in --project-path first, then CWD (covers running from the project root).
    for base in ([project_path] if project_path else []) + [os.getcwd()]:
        hook_sid, hook_tp = _read_last_session_file(base)
        if hook_tp:
            # If caller specified a session_id, honour it: skip if it doesn't match.
            if session_id and hook_sid != session_id:
                break  # mismatch — fall through to search below
            return hook_tp, None

    root = find_claude_root()
    projects = os.path.join(root, "projects")
    if not os.path.isdir(projects):
        return None, f"no {projects} dir (is Claude Code installed / has it run?)"

    candidates = glob.glob(os.path.join(projects, "*", "**", "*.jsonl"), recursive=True)
    if not candidates:
        return None, "no session .jsonl files found"

    # explicit session id wins
    if session_id:
        for c in candidates:
            if os.path.basename(c).startswith(session_id):
                return c, None
        return None, f"session id {session_id} not found under {projects}"

    # narrow to a project path if given — then pick the most recent session there
    if project_path:
        ap = encode_project_path(project_path)
        slug = ap.strip("/").replace("/", "-")
        proj_dirs = [
            d
            for d in glob.glob(os.path.join(projects, "*"))
            if slug in os.path.basename(d) or os.path.basename(ap) in os.path.basename(d)
        ]
        scoped = []
        for d in proj_dirs:
            scoped += glob.glob(os.path.join(d, "**", "*.jsonl"), recursive=True)
        if scoped:
            candidates = scoped
        return max(candidates, key=os.path.getmtime), None

    # No session_id, no project_path, and last-session.json wasn't available.
    # Picking the globally-latest JSONL would silently include another session's tokens —
    # refusing is safer than a silent wrong answer.
    return None, (
        "session cannot be auto-located — run within story-implement (Stop hook writes "
        ".claude/last-session.json automatically) or pass --session-id <id> or "
        "--project-path <repo>"
    )


# ---------------------------------------------------------------------------
# JSONL parsing — two views: aggregate (per_model) and turn-level
# ---------------------------------------------------------------------------


def iter_turns(path):
    """Yield per-turn dicts from a session JSONL, deduped by requestId.

    Each dict: {turn, model, input, output, cache_read, cache_create, snippet}
    """
    seen = set()
    turn_n = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") != "assistant":
                continue
            msg = o.get("message", {})
            usage = msg.get("usage")
            if not usage:
                continue
            rid = o.get("requestId") or msg.get("id") or o.get("uuid")
            if rid in seen:
                continue
            seen.add(rid)
            turn_n += 1

            # grab first text block as a context hint
            snippet = ""
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    snippet = block.get("text", "")[:120].replace("\n", " ").strip()
                    break

            yield {
                "turn": turn_n,
                "model": model_family(msg.get("model", "")),
                "input": usage.get("input_tokens", 0) or 0,
                "output": usage.get("output_tokens", 0) or 0,
                "cache_read": usage.get("cache_read_input_tokens", 0) or 0,
                "cache_create": usage.get("cache_creation_input_tokens", 0) or 0,
                "snippet": snippet,
            }


def sum_jsonl(path):
    """Aggregate per-model totals from a session JSONL (existing behaviour)."""
    per = defaultdict(lambda: defaultdict(int))
    for t in iter_turns(path):
        fam = t["model"]
        per[fam]["input"] += t["input"]
        per[fam]["output"] += t["output"]
        per[fam]["cache_read"] += t["cache_read"]
        per[fam]["cache_create"] += t["cache_create"]
    return per


# ---------------------------------------------------------------------------
# Pricing helpers
# ---------------------------------------------------------------------------


def price(fam, toks):
    """Cost in USD for one model family's token dict."""
    r = RATES.get(fam)
    if not r:
        return None
    inp = r["input"] / 1_000_000
    out = r["output"] / 1_000_000
    return (
        toks["input"] * inp
        + toks["output"] * out
        + toks["cache_read"] * inp * CACHE_READ_MULT
        + toks["cache_create"] * inp * CACHE_CREATE_MULT
    )


def turn_cost(t):
    """Cost in USD for a single turn dict."""
    r = RATES.get(t["model"], {"input": 3.0, "output": 15.0})
    inp = r["input"] / 1_000_000
    out = r["output"] / 1_000_000
    return (
        t["input"] * inp
        + t["output"] * out
        + t["cache_read"] * inp * CACHE_READ_MULT
        + t["cache_create"] * inp * CACHE_CREATE_MULT
    )


# ---------------------------------------------------------------------------
# Phase detection — groups turns separated by /compact context resets
# ---------------------------------------------------------------------------


def detect_phases(turns):
    """Group turns into compact-reset phases.

    A new phase starts when cache_read drops below _PHASE_RESET_RATIO of the
    running max in the current phase (and the current phase grew past
    _PHASE_RESET_MIN_MAX). This reliably catches the post-/compact rebuild
    pattern (cache reads drop from 50K–100K back to ~12K) without
    false-positives from normal within-phase variation.

    Returns a list of turn-lists, one per phase.
    """
    if not turns:
        return []

    phases = []
    current = [turns[0]]
    running_max = turns[0]["cache_read"]

    for t in turns[1:]:
        cr = t["cache_read"]
        is_reset = running_max >= _PHASE_RESET_MIN_MAX and cr < running_max * _PHASE_RESET_RATIO
        if is_reset:
            phases.append(current)
            current = [t]
            running_max = cr
        else:
            current.append(t)
            running_max = max(running_max, cr)

    phases.append(current)
    return phases


def summarise_phase(phase_turns, phase_num):
    """Return a summary dict for one phase."""
    start = phase_turns[0]["turn"]
    end = phase_turns[-1]["turn"]
    count = len(phase_turns)

    total_out = sum(t["output"] for t in phase_turns)
    total_cr = sum(t["cache_read"] for t in phase_turns)
    total_cc = sum(t["cache_create"] for t in phase_turns)
    avg_cr = round(total_cr / count) if count else 0
    cost = sum(turn_cost(t) for t in phase_turns)

    # first non-empty snippet as a context hint
    hint = next((t["snippet"] for t in phase_turns if t["snippet"]), "")

    return {
        "phase": phase_num,
        "turns": f"{start}-{end}",
        "turn_count": count,
        "hint": hint[:100],
        "output_tokens": total_out,
        "cache_read_tokens": total_cr,
        "cache_create_tokens": total_cc,
        "avg_cache_read_per_turn": avg_cr,
        "cost_usd": round(cost, 4),
    }


# ---------------------------------------------------------------------------
# Cost-driver breakdown — what category of spend dominates
# ---------------------------------------------------------------------------


def compute_cost_drivers(turns):
    """Break total spend into four components across all turns.

    Returns a dict with cache_reads, cache_writes, output, input each having
    cost_usd and pct keys.
    """
    comp = defaultdict(float)
    grand = 0.0

    for t in turns:
        r = RATES.get(t["model"], {"input": 3.0, "output": 15.0})
        inp = r["input"] / 1_000_000
        out = r["output"] / 1_000_000

        c_in = t["input"] * inp
        c_out = t["output"] * out
        c_cr = t["cache_read"] * inp * CACHE_READ_MULT
        c_cc = t["cache_create"] * inp * CACHE_CREATE_MULT

        comp["input"] += c_in
        comp["output"] += c_out
        comp["cache_reads"] += c_cr
        comp["cache_writes"] += c_cc
        grand += c_in + c_out + c_cr + c_cc

    def entry(key):
        v = comp[key]
        return {"cost_usd": round(v, 4), "pct": round(v / grand * 100, 1) if grand else 0}

    return {
        "cache_reads": entry("cache_reads"),  # re-reading context each turn
        "cache_writes": entry("cache_writes"),  # first-read of docs / new context
        "output": entry("output"),  # model generating text/code
        "input": entry("input"),  # raw prompt tokens each turn
    }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def build_report(per_model, source, session_file, turns=None, per_skill=None):
    rows = []
    total_tokens = 0
    total_cost = 0.0
    cost_known = True
    use_otel_cost = source.startswith("otel")

    for fam in sorted(per_model):
        t = per_model[fam]
        toks = t["input"] + t["output"] + t["cache_read"] + t["cache_create"]
        if use_otel_cost and "_otel_cost_usd" in t:
            # Use billing-exact cost from OTEL metric directly
            c = t["_otel_cost_usd"]
        else:
            c = price(fam, t)
        rows.append(
            {
                "model": fam,
                "input_tokens": t["input"],
                "output_tokens": t["output"],
                "cache_read_tokens": t["cache_read"],
                "cache_create_tokens": t["cache_create"],
                "tokens": toks,
                "cost_usd": round(c, 4) if c is not None else None,
            }
        )
        total_tokens += toks
        if c is None:
            cost_known = False
        else:
            total_cost += c

    report = {
        "source": source,
        "session_file": session_file,
        "per_model": rows,
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 4) if cost_known else None,
        "cost_complete": cost_known,
        # billing-accurate when the cost came from Claude Code's engine (OTEL); otherwise the
        # total is a local JSONL price ESTIMATE and must not be written to the Jira cost field.
        "cost_is_estimate": not use_otel_cost,
    }

    # Skill-wise breakdown from OTEL (only when skill.name attribute is captured)
    if per_skill is not None:
        report["per_skill"] = per_skill

    # Phase + cost-driver analysis only available from JSONL (need turn-level data)
    if turns is not None:
        phases = detect_phases(turns)
        report["per_phase"] = [summarise_phase(p, i + 1) for i, p in enumerate(phases)]
        report["cost_drivers"] = compute_cost_drivers(turns)

    # customfield_10138: compact JSON string for the Jira tokens-usage paragraph field.
    # Teams scope stories with JQL then read this field via REST — no comment parsing needed.
    _breakdown = {
        "total_cost_usd": report["total_cost_usd"],
        "total_tokens": report["total_tokens"],
        "per_model": rows,
        "source": source,
    }
    if "per_skill" in report:
        _breakdown["per_skill"] = report["per_skill"]
    if "per_phase" in report:
        _breakdown["per_phase"] = report["per_phase"]
        _breakdown["cost_drivers"] = report["cost_drivers"]
    report["customfield_10138"] = json.dumps(_breakdown, separators=(",", ":"))

    return report


def caveat_for(source):
    if source == "otel":
        return (
            "OTEL-derived from claude_code.cost.usage metric (otel_receiver.py running). "
            "Cost comes directly from Claude Code's billing engine."
        )
    if source == "otel+jsonl":
        return (
            "Cost totals from OTEL claude_code.cost.usage (Claude Code billing engine). "
            "Per-phase and cost-driver breakdown from JSONL turn data."
        )
    if source == "statusbar":
        return "Statusbar total from /usage export — same underlying API usage data."
    if source == "statusbar+jsonl":
        return (
            "Cost totals from statusbar /usage export. Per-phase and cost-driver "
            "breakdown from JSONL turn data."
        )
    return (
        "JSONL-derived (deduped by requestId). output_tokens already includes "
        "extended-thinking tokens per the Anthropic API spec — this is the billing-accurate "
        "total. Cost accuracy vs. statusbar/OTEL depends on turn attribution scope."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _load_otel_costs(session_id):
    """Read ~/.claude/otel-costs.json written by otel_receiver.py.

    Returns (per_model, per_skill) if session found, else None.
      per_model: same shape as JSONL aggregate (model → token/cost totals)
      per_skill: list of {skill, cost_usd, tokens, models[]} sorted by cost desc,
                 or None when skill.name not captured in OTEL data
    otel-costs.json schema:
      {<session_id>: {<model>: {cost_usd, tokens}, "__skills": {<skill>: {<model>: ...}}}}
    """
    otel_file = os.path.join(find_claude_root(), "otel-costs.json")
    if not os.path.exists(otel_file):
        return None
    try:
        data = json.load(open(otel_file))
    except Exception:
        return None

    # __meta carries receiver_version/reset_at — not a session. Pop it before lookup.
    meta = data.pop("__meta", None) if isinstance(data, dict) else None

    # If explicit session_id given, look it up; otherwise fall back to last-session.json sid
    if session_id and session_id in data:
        entry = data[session_id]
    elif not session_id and data:
        lsf_sid, _ = _read_last_session_file(os.getcwd())
        entry = data.get(lsf_sid) if lsf_sid else None
        if entry is None:
            return None
    else:
        return None
    # never mistake the meta key for a session
    if session_id == "__meta" or entry is meta:
        return None

    # Convert to per_model dict (same shape as JSONL aggregate).
    # Keys starting with "__" are metadata (e.g. __skills) — skip them here.
    per_model = defaultdict(lambda: defaultdict(int))
    for fam, rec in entry.items():
        if fam.startswith("__"):
            continue
        fam = model_family(fam)
        toks = rec.get("tokens", {})
        per_model[fam]["input"] += toks.get("input", 0)
        per_model[fam]["output"] += toks.get("output", 0)
        per_model[fam]["cache_read"] += toks.get("cacheRead", 0)
        per_model[fam]["cache_create"] += toks.get("cacheCreation", 0)
        per_model[fam]["_otel_cost_usd"] = rec.get("cost_usd", 0.0)

    if not per_model:
        return None

    # Extract per-skill breakdown from the __skills subtree (populated when skill.name
    # attribute is present on the OTEL metric datapoints).
    per_skill = None
    raw_skills = entry.get("__skills")
    if raw_skills:
        per_skill = []
        for skill_name, skill_models in raw_skills.items():
            skill_cost = 0.0
            skill_tokens = 0
            model_rows = []
            for mdl, rec in skill_models.items():
                mdl_fam = model_family(mdl)
                toks = rec.get("tokens", {})
                t = (
                    toks.get("input", 0)
                    + toks.get("output", 0)
                    + toks.get("cacheRead", 0)
                    + toks.get("cacheCreation", 0)
                )
                c = rec.get("cost_usd", 0.0)
                skill_cost += c
                skill_tokens += t
                model_rows.append({"model": mdl_fam, "tokens": t, "cost_usd": round(c, 4)})
            per_skill.append(
                {
                    "skill": skill_name,
                    "cost_usd": round(skill_cost, 4),
                    "tokens": skill_tokens,
                    "models": model_rows,
                }
            )
        per_skill.sort(key=lambda x: x["cost_usd"], reverse=True)

    return per_model, per_skill


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--story", required=True, help="story key, e.g. LNPRTL-30")
    ap.add_argument("--session-id", help="explicit Claude Code session id (most reliable)")
    ap.add_argument("--project-path", help="repo path to scope session lookup")
    ap.add_argument(
        "--statusbar-json",
        help="path to a statusbar JSON with cumulative totals "
        "(more accurate; if present, used as the source). Expected keys: "
        "total_input_tokens, total_output_tokens, optionally per-model.",
    )
    ap.add_argument("--pretty", action="store_true", help="pretty-print JSON")
    args = ap.parse_args()

    source = None
    per_model = None
    otel_per_skill = None
    session_file = None
    turns = None

    # Step 0 — OTEL: billing-exact (includes extended-thinking tokens).
    # Written by otel_receiver.py from claude_code.cost.usage metric.
    otel_result = _load_otel_costs(args.session_id)
    if otel_result is not None:
        per_model, otel_per_skill = otel_result
        source = "otel"

    # Step 1 — statusbar: accurate billing totals (includes extended-thinking tokens).
    # Used for per_model, total_cost_usd, and customfield_10105 when available.
    if args.statusbar_json and os.path.exists(args.statusbar_json):
        try:
            sb = json.load(open(args.statusbar_json))
            if "per_model" in sb:
                per_model = defaultdict(lambda: defaultdict(int))
                for fam, t in sb["per_model"].items():
                    f = model_family(fam)
                    per_model[f]["input"] += t.get("input_tokens", 0)
                    per_model[f]["output"] += t.get("output_tokens", 0)
                    per_model[f]["cache_read"] += t.get("cache_read_input_tokens", 0)
                    per_model[f]["cache_create"] += t.get("cache_creation_input_tokens", 0)
            else:
                per_model = {
                    "unknown": {
                        "input": sb.get("total_input_tokens", 0),
                        "output": sb.get("total_output_tokens", 0),
                        "cache_read": sb.get("total_cache_read_tokens", 0),
                        "cache_create": sb.get("total_cache_creation_tokens", 0),
                    }
                }
            source = "statusbar"
        except Exception as e:
            print(
                f"warn: could not read statusbar-json ({e}); falling back to JSONL", file=sys.stderr
            )

    # Step 2 — JSONL: turn-level data for per_phase + cost_drivers breakdown.
    # Always attempted when a session locator is given — independent of otel/statusbar.
    # Also serves as the per_model/totals fallback when neither otel nor statusbar available.
    if args.session_id or args.project_path or per_model is None:
        session_file, err = locate_session(args.session_id, args.project_path)
        if session_file:
            turns = list(iter_turns(session_file))
            if per_model is None:
                # no statusbar — derive per_model and totals from JSONL
                if not turns:
                    print(f"ERROR: no usage found in {session_file}", file=sys.stderr)
                    return 1
                source = "jsonl"
                per_model = defaultdict(lambda: defaultdict(int))
                for t in turns:
                    fam = t["model"]
                    per_model[fam]["input"] += t["input"]
                    per_model[fam]["output"] += t["output"]
                    per_model[fam]["cache_read"] += t["cache_read"]
                    per_model[fam]["cache_create"] += t["cache_create"]
        elif per_model is None:
            # JSONL also failed and we have no statusbar — hard stop
            print(f"ERROR: {err}", file=sys.stderr)
            return 1

    if per_model is None:
        print("ERROR: no cost data — provide --statusbar-json and/or --session-id", file=sys.stderr)
        return 1

    # Composite source labels when multiple sources contribute
    if source == "otel" and turns:
        source = "otel+jsonl"
    elif source == "statusbar" and turns:
        source = "statusbar+jsonl"

    report = build_report(per_model, source, session_file, turns=turns, per_skill=otel_per_skill)
    report["story"] = args.story
    report["caveat"] = caveat_for(source)

    # Option B — the number written to Jira must come from Claude Code's billing engine
    # (OTEL claude_code.cost.usage), never from local re-pricing. If OTEL cost isn't
    # available (receiver not running / not ready), DO NOT write the cost field — instead
    # signal that story-implement should post a Jira COMMENT explaining the gap.
    otel_cost_available = source.startswith("otel") and report.get("total_cost_usd") is not None
    if otel_cost_available:
        report["customfield_10105"] = report["total_cost_usd"]  # Jira number field
        report["jira_cost_action"] = "write_field"
    else:
        # No billing-engine cost. Any total present here is a local JSONL ESTIMATE — not
        # accurate enough for the cost field. Leave the field unset and tell the caller to
        # post a comment instead of writing a wrong number.
        report["customfield_10105"] = None
        report["jira_cost_action"] = "post_comment"
        report["jira_cost_comment"] = (
            "Token cost not recorded for this story: the OpenTelemetry receiver "
            "(otel_receiver.py) was not running or had no data for this session, so a "
            "billing-accurate cost from Claude Code's engine was unavailable. "
            + (
                f"A rough local JSONL estimate of ~${report['total_cost_usd']:.2f} was computed "
                "but NOT written to the cost field to avoid recording an inaccurate figure. "
                if report.get("total_cost_usd") is not None
                else ""
            )
            + "Start the receiver before the next run to capture cost."
        )

    print(json.dumps(report, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
