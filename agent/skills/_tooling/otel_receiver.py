#!/usr/bin/env python3
"""
otel_receiver.py — minimal OTLP HTTP/JSON receiver for Claude Code telemetry.

Listens on localhost:4318 for OTLP HTTP/JSON payloads from Claude Code (requires
CLAUDE_CODE_ENABLE_TELEMETRY=1 plus OTEL_* env vars — see README).

What it captures:
  - claude_code.cost.usage  metric  → billing-accurate USD per model, per session
  - claude_code.token.usage metric  → token counts by type (input/output/cacheRead/cacheCreation)
  - claude_code.api_request event   → per-request cost_usd (backup cross-check)

Persists to ~/.claude/otel-costs.json so story_tokens.py can read it.

Run as a background process BEFORE starting Claude Code:
  python3 .claude/skills/_tooling/otel_receiver.py &

Stop it when done, or let it run persistently between sessions.

Output file schema (~/.claude/otel-costs.json):
  {
    "<session_id>": {
      "<model>": {
        "cost_usd": 0.0,
        "tokens": {
          "input": 0, "output": 0, "cacheRead": 0, "cacheCreation": 0
        }
      }
    }
  }
"""

import gzip
import http.server
import json
import os
import sys
import threading
import time

PORT = int(os.environ.get("OTEL_RECEIVER_PORT", 4318))
OUT_FILE = os.path.join(os.path.expanduser("~/.claude"), "otel-costs.json")
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# OTLP JSON payload parsers
# ---------------------------------------------------------------------------


def _attr_val(attr):
    """Extract string/double/int value from an OTLP attribute dict."""
    v = attr.get("value", {})
    return (
        v.get("stringValue")
        or v.get("doubleValue")
        or v.get("intValue")
        or v.get("asDouble")
        or v.get("asInt")
    )


def _attrs_to_dict(attrs):
    return {a["key"]: _attr_val(a) for a in (attrs or [])}


def _process_metrics(payload, store):
    """Parse resourceMetrics → update store with cost.usage + token.usage."""
    for rm in payload.get("resourceMetrics", []):
        res_attrs = _attrs_to_dict(rm.get("resource", {}).get("attributes", []))
        for sm in rm.get("scopeMetrics", []):
            for metric in sm.get("metrics", []):
                name = metric.get("name", "")
                if name not in ("claude_code.cost.usage", "claude_code.token.usage"):
                    continue

                # Metrics are counters (Sum); grab dataPoints + temporality.
                # aggregationTemporality: 1 = DELTA (default), 2 = CUMULATIVE.
                # DELTA → each datapoint is an interval increment, must be SUMMED.
                # CUMULATIVE → each datapoint is a running total, take the MAX.
                sum_block = metric.get("sum", {})
                temporality = sum_block.get("aggregationTemporality", 1)  # default DELTA
                is_cumulative = temporality == 2
                data_points = sum_block.get("dataPoints", []) or metric.get("gauge", {}).get(
                    "dataPoints", []
                )
                for dp in data_points:
                    attrs = _attrs_to_dict(dp.get("attributes", []))
                    # session.id may be on resource or datapoint
                    sid = attrs.get("session.id") or res_attrs.get("session.id", "unknown")
                    model = _normalise_model(attrs.get("model", "unknown"))
                    skill = attrs.get("skill.name") or attrs.get("skill")
                    value = dp.get("asDouble") or dp.get("asInt") or dp.get("value") or 0

                    _ensure(store, sid, model)
                    if name == "claude_code.cost.usage":
                        _accumulate(store[sid][model], "cost_usd", float(value), is_cumulative)
                        if skill:
                            _ensure_skill(store, sid, skill, model)
                            _accumulate(
                                store[sid]["__skills"][skill][model],
                                "cost_usd",
                                float(value),
                                is_cumulative,
                            )
                    elif name == "claude_code.token.usage":
                        # type: input / output / cacheRead / cacheCreation
                        tok_type = attrs.get("type", "input")
                        _accumulate_tok(
                            store[sid][model]["tokens"], tok_type, int(value), is_cumulative
                        )
                        if skill:
                            _ensure_skill(store, sid, skill, model)
                            _accumulate_tok(
                                store[sid]["__skills"][skill][model]["tokens"],
                                tok_type,
                                int(value),
                                is_cumulative,
                            )


def _process_logs(payload, store):
    """Parse resourceLogs → look for api_request events with cost_usd."""
    for rl in payload.get("resourceLogs", []):
        res_attrs = _attrs_to_dict(rl.get("resource", {}).get("attributes", []))
        for sl in rl.get("scopeLogs", []):
            for record in sl.get("logRecords", []):
                attrs = _attrs_to_dict(record.get("attributes", []))
                if attrs.get("event.name") != "api_request":
                    continue

                sid = attrs.get("session.id") or res_attrs.get("session.id", "unknown")
                model = _normalise_model(attrs.get("model", "unknown"))
                cost = attrs.get("cost_usd")
                if cost is None:
                    continue

                _ensure(store, sid, model)
                # api_request events are per-request — accumulate
                store[sid][model]["cost_usd_from_events"] = store[sid][model].get(
                    "cost_usd_from_events", 0.0
                ) + float(cost)
                # Also accumulate token counts from events as a fallback
                for key, tok_type in [
                    ("input_tokens", "input"),
                    ("output_tokens", "output"),
                    ("cache_read_tokens", "cacheRead"),
                    ("cache_creation_tokens", "cacheCreation"),
                ]:
                    n = attrs.get(key)
                    if n is not None:
                        store[sid][model]["tokens"][tok_type] = store[sid][model]["tokens"].get(
                            tok_type, 0
                        ) + int(n)


def _accumulate(rec, key, value, is_cumulative):
    """Accumulate a metric value honoring OTLP temporality.

    DELTA (default): each export is an interval increment → SUM them.
    CUMULATIVE: each export is a running total → keep the MAX (monotonic).
    This is the core fix: Claude Code exports DELTA by default, so cost/token
    datapoints MUST be summed. Taking max() (the old bug) kept only the single
    largest interval and undercounted the total by ~the number of intervals.
    """
    cur = rec.get(key, 0.0)
    rec[key] = max(cur, value) if is_cumulative else (cur + value)


def _accumulate_tok(tokens, tok_type, value, is_cumulative):
    cur = tokens.get(tok_type, 0)
    tokens[tok_type] = max(cur, value) if is_cumulative else (cur + value)


def _normalise_model(m):
    """claude-sonnet-4-6-... → sonnet, etc."""
    if not m:
        return "unknown"
    s = m.lower()
    for fam in ("opus", "sonnet", "haiku"):
        if fam in s:
            return fam
    return m


def _ensure(store, sid, model):
    store.setdefault(sid, {})
    # __meta holds receiver_version/reset_at — never treat it as a model bucket
    if sid == "__meta":
        return
    store[sid].setdefault(model, {"cost_usd": 0.0, "tokens": {}})


def _ensure_skill(store, sid, skill, model):
    store[sid].setdefault("__skills", {})
    store[sid]["__skills"].setdefault(skill, {})
    store[sid]["__skills"][skill].setdefault(model, {"cost_usd": 0.0, "tokens": {}})


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _load_store():
    try:
        return json.load(open(OUT_FILE))
    except Exception:
        return {}


def _save_store(store):
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, OUT_FILE)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

_store = _load_store()
_dirty = False


class OTLPHandler(http.server.BaseHTTPRequestHandler):
    def _read_chunked(self):
        """Minimal chunked-transfer-encoding reader (stdlib http.server doesn't
        dechunk automatically, and Node's OTLP exporter may not send Content-Length)."""
        chunks = []
        while True:
            size_line = self.rfile.readline().strip()
            size = int(size_line.split(b";")[0], 16)
            if size == 0:
                self.rfile.readline()  # trailing CRLF after last chunk
                break
            chunks.append(self.rfile.read(size))
            self.rfile.readline()  # CRLF after chunk data
        return b"".join(chunks)

    def _read_body(self):
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            body = self._read_chunked()
        else:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
        if "gzip" in self.headers.get("Content-Encoding", "").lower():
            body = gzip.decompress(body)
        return body

    def do_POST(self):
        global _dirty
        try:
            body = self._read_body()
            payload = json.loads(body)

            with _lock:
                if self.path == "/v1/metrics":
                    _process_metrics(payload, _store)
                elif self.path == "/v1/logs":
                    _process_logs(payload, _store)
                _dirty = True

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"partialSuccess":{}}')
        except Exception as e:
            self.send_response(400)
            self.end_headers()
            print(
                f"[otel_receiver] parse error: {e} "
                f"(path={self.path}, Content-Encoding={self.headers.get('Content-Encoding')}, "
                f"Transfer-Encoding={self.headers.get('Transfer-Encoding')}, "
                f"Content-Length={self.headers.get('Content-Length')})",
                file=sys.stderr,
            )

    def log_message(self, fmt, *args):
        pass  # suppress per-request logs; periodic summary is enough


# ---------------------------------------------------------------------------
# Background flush thread
# ---------------------------------------------------------------------------


def _flush_loop(interval=10):
    """Flush store to disk every interval seconds when dirty."""
    global _dirty
    while True:
        time.sleep(interval)
        with _lock:
            if _dirty:
                try:
                    _save_store(_store)
                    _dirty = False
                except Exception as e:
                    print(f"[otel_receiver] flush error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    t = threading.Thread(target=_flush_loop, daemon=True)
    t.start()

    server = http.server.HTTPServer(("localhost", PORT), OTLPHandler)
    print(f"[otel_receiver] listening on localhost:{PORT}  output → {OUT_FILE}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        with _lock:
            _save_store(_store)
        print("\n[otel_receiver] flushed and stopped.", flush=True)


if __name__ == "__main__":
    main()
