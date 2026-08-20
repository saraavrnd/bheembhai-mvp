"""Unit tests — agent.log JSONL → typed transcript blocks.

Synthetic stream-json fixtures exercise the tolerant parser: agent.log is
heartbeat-uploaded while the CLI is still writing, so real content ends
mid-line, starts mid-event, and interleaves plain wrapper lines with JSON.
"""

import json

from platform_api.agent_log import (
    _TOOL_OUTPUT_SUMMARY_MAX,
    _TOOL_RAW_MAX,
    _TOOL_SUMMARY_MAX,
    LogBlock,
    parse_agent_log,
    strip_ansi,
    summarize_tool_input,
    summarize_tool_output,
)

# ── helpers ─────────────────────────────────────────────────────────────

def J(**kw):
    return json.dumps(kw)


def logtext(*lines):
    return "\n".join(lines)


def kinds(result):
    return [b.kind for b in result.blocks]


# ── happy path + result dedup ───────────────────────────────────────────

def test_happy_path_result_dedup():
    text = logtext(
        "=== running implement ===",
        J(type="assistant", message={"content": [
            {"type": "text", "text": "Let me check the code."}]}),
        J(type="assistant", message={"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "ls -la"}}]}),
        J(type="user", message={"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "total 4\n-rw-r--r-- 1 node node 0 file.txt",
             "is_error": False}]}),
        J(type="assistant", message={"content": [
            {"type": "text", "text": "Final answer here."}]}),
        J(type="result", result="Final answer here.",
          total_cost_usd=0.01, is_error=False),
    )
    result = parse_agent_log(text)
    assert kinds(result) == ["plain", "assistant_text", "tool_call",
                             "tool_result", "assistant_text"]
    assert result.blocks[0].text == "=== running implement ==="
    call = result.blocks[2]
    assert call.tool_name == "Bash"
    assert call.tool_id == "t1"
    assert call.tool_summary == "ls -la"
    assert call.raw == '{"command": "ls -la"}'
    res = result.blocks[3]
    assert res.tool_id == "t1"
    assert res.tool_status == "ok"
    assert res.tool_summary == "total 4 -rw-r--r-- 1 node node 0 file.txt"
    assert result.blocks[4].text == "Final answer here."
    assert result.events_seen == 5
    assert result.lines_skipped == 0
    assert result.truncated is False


# ── plain wrapper lines ─────────────────────────────────────────────────

def test_plain_lines_coalesce_consecutive_runs():
    text = logtext(
        "=== running implement ===",
        "cloned existing run branch",
        "pushed to feat/x",
        J(type="assistant", message={"content": [
            {"type": "text", "text": "hi"}]}),
        "after line one",
        "after line two",
    )
    result = parse_agent_log(text)
    assert kinds(result) == ["plain", "assistant_text", "plain"]
    assert result.blocks[0].text == (
        "=== running implement ===\ncloned existing run branch\n"
        "pushed to feat/x")
    assert result.blocks[2].text == "after line one\nafter line two"


def test_non_streaming_fallback_blob_single_plain_block():
    text = logtext(
        "(streaming unavailable — output will appear when the agent finishes)",
        "plain reply line 1",
        "plain reply line 2",
        "plain reply line 3",
    )
    result = parse_agent_log(text)
    assert kinds(result) == ["plain"]
    assert result.blocks[0].text.count("\n") == 3


def test_empty_and_whitespace_only_logs():
    assert parse_agent_log("").blocks == []
    assert parse_agent_log("   \n  \n").blocks == []


# ── robustness: truncated / mid-stream content ──────────────────────────

def test_truncated_final_json_line_skipped():
    text = logtext(
        J(type="assistant", message={"content": [
            {"type": "text", "text": "before"}]}),
        '{"type":"assistant","mess',   # heartbeat upload cut mid-event
    )
    result = parse_agent_log(text)
    assert kinds(result) == ["assistant_text"]
    assert result.lines_skipped == 1


def test_mid_stream_start_fragment_and_orphan_tool_result():
    text = logtext(
        '"text":"cut-off"}',            # window opened mid-event
        J(type="user", message={"content": [
            {"type": "tool_result", "tool_use_id": "gone",
             "content": "orphan output", "is_error": True}]}),
        J(type="assistant", message={"content": [
            {"type": "text", "text": "back on track"}]}),
    )
    result = parse_agent_log(text)
    assert kinds(result) == ["tool_result", "assistant_text"]
    orphan = result.blocks[0]
    assert orphan.tool_id == "gone"
    assert orphan.tool_status == "error"
    assert orphan.tool_summary == "orphan output"


def test_unknown_event_types_and_non_dict_json_skipped():
    text = logtext(
        J(type="system", subtype="init"),
        J(type="stream_event"),
        "[1, 2, 3]",
        J(type="user", message={"content": [
            {"type": "text", "text": "prompt echo — noise"}]}),
        J(type="assistant", message={"content": [
            {"type": "text", "text": "kept"}]}),
    )
    result = parse_agent_log(text)
    assert kinds(result) == ["assistant_text"]
    assert result.blocks[0].text == "kept"
    assert result.lines_skipped == 3      # system, stream_event, non-dict
    assert result.events_seen == 2        # user + assistant


def test_missing_fields_tolerated():
    text = logtext(
        J(type="assistant"),                          # no message
        J(type="user"),                               # no message
        J(type="assistant", message={"content": "bare string"}),
        J(type="user", message="bare string"),
        J(type="result"),                             # no result, no error
        J(type="assistant", message={"content": [
            {"type": "text", "text": "survives"}]}),
    )
    result = parse_agent_log(text)
    assert kinds(result) == ["assistant_text"]
    assert result.blocks[0].text == "survives"


def test_ansi_sequences_stripped():
    text = logtext(
        "\x1b[32mgreen\x1b[0m plain line",
        J(type="assistant", message={"content": [
            {"type": "text", "text": "spinner \x1b[?25ltext"}]}),
    )
    result = parse_agent_log(text)
    assert result.blocks[0].text == "green plain line"
    assert result.blocks[1].text == "spinner text"


def test_crlf_and_cr_normalized():
    text = "line1\r\nline2\rline3"
    result = parse_agent_log(text)
    assert result.blocks[0].text == "line1\nline2\nline3"


def test_strip_ansi_lone_escape():
    assert strip_ansi("a\x1bb") == "ab"


# ── result event handling ───────────────────────────────────────────────

def test_result_dedup_whitespace_only_difference():
    text = logtext(
        J(type="assistant", message={"content": [
            {"type": "text", "text": "Final answer\nhere."}]}),
        J(type="result", result="Final answer here.", is_error=False),
    )
    result = parse_agent_log(text)
    assert kinds(result) == ["assistant_text"]     # wrapped text deduped


def test_result_differing_text_emitted():
    text = logtext(
        J(type="assistant", message={"content": [
            {"type": "text", "text": "Final answer here."}]}),
        J(type="result", result="A different final reply.", is_error=False),
    )
    result = parse_agent_log(text)
    assert kinds(result) == ["assistant_text", "result_text"]
    assert result.blocks[1].text == "A different final reply."
    assert result.blocks[1].error is False


def test_result_without_prior_assistant_emitted():
    text = logtext(J(type="result", result="Only the tail.", is_error=False))
    result = parse_agent_log(text)
    assert kinds(result) == ["result_text"]
    assert result.blocks[0].text == "Only the tail."


def test_error_result_flags_and_error_message_fallback():
    text = logtext(
        J(type="result", result="Something broke.", is_error=True),
    )
    result = parse_agent_log(text)
    assert kinds(result) == ["result_text"]
    assert result.blocks[0].error is True

    # An error result with no .result text uses the error message instead.
    result = parse_agent_log(
        J(type="result", is_error=True, error={"message": "boom"}))
    assert kinds(result) == ["result_text"]
    assert result.blocks[0].text == "boom"
    assert result.blocks[0].error is True


# ── tool blocks ─────────────────────────────────────────────────────────

def test_multiple_tool_blocks_per_message_order_preserved():
    text = logtext(
        J(type="assistant", message={"content": [
            {"type": "tool_use", "id": "a", "name": "Read",
             "input": {"file_path": "one.py"}},
            {"type": "tool_use", "id": "b", "name": "Grep",
             "input": {"pattern": "TODO"}},
        ]}),
        J(type="user", message={"content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "one",
             "is_error": False},
            {"type": "tool_result", "tool_use_id": "b", "content": "two",
             "is_error": False},
        ]}),
    )
    result = parse_agent_log(text)
    assert kinds(result) == ["tool_call", "tool_call",
                             "tool_result", "tool_result"]
    assert [b.tool_id for b in result.blocks] == ["a", "b", "a", "b"]
    assert result.blocks[0].tool_summary == "one.py"
    assert result.blocks[1].tool_summary == "TODO"


def test_tool_result_is_error_flag():
    result = parse_agent_log(J(type="user", message={"content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "boom",
         "is_error": True}]}))
    assert result.blocks[0].tool_status == "error"
    # Missing is_error defaults to ok.
    result = parse_agent_log(J(type="user", message={"content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "fine"}]}))
    assert result.blocks[0].tool_status == "ok"


# ── summaries ───────────────────────────────────────────────────────────

def test_tool_input_summary_preferred_keys_and_fallbacks():
    assert summarize_tool_input("Bash", {"command": "ls -la /tmp"}) \
        == "ls -la /tmp"
    assert summarize_tool_input("Read", {"file_path": "src/a.py"}) \
        == "src/a.py"
    assert summarize_tool_input("WebSearch", {"query": "why is the sky blue"}) \
        == "why is the sky blue"
    # Fallback: first string value.
    assert summarize_tool_input("MysteryTool", {"arbitrary": "hello"}) \
        == "hello"
    # Fallback: no strings → compact JSON.
    assert summarize_tool_input("MysteryTool", {"count": 3}) \
        == '{"count": 3}'
    assert summarize_tool_input("Bash", None) == ""
    assert summarize_tool_input("Bash", "raw string") == "raw string"


def test_tool_input_summary_newline_collapse_and_truncation():
    assert summarize_tool_input("Bash", {"command": "a\nb\n  c"}) == "a b c"
    long = summarize_tool_input("Bash", {"command": "x" * 200})
    assert long == "x" * _TOOL_SUMMARY_MAX + "…"


def test_tool_output_string_and_content_block_forms():
    assert summarize_tool_output("hello") == "hello"
    assert summarize_tool_output(
        [{"type": "text", "text": "line1\nline2"}]) == "line1 line2"
    # Non-text content items (images, nested tool_use) are skipped.
    assert summarize_tool_output(
        [{"type": "image", "source": {}},
         {"type": "text", "text": "ok"}]) == "ok"
    assert summarize_tool_output(None) == ""


def test_tool_output_summary_truncation():
    summary = summarize_tool_output("word " * 100)
    assert summary == " ".join(["word"] * 100)[:_TOOL_OUTPUT_SUMMARY_MAX] + "…"


def test_tool_raw_caps():
    result = parse_agent_log(J(type="user", message={"content": [
        {"type": "tool_result", "tool_use_id": "t",
         "content": "x" * (_TOOL_RAW_MAX + 5000)}]}))
    block = result.blocks[0]
    assert len(block.raw) == _TOOL_RAW_MAX + len("\n…[truncated]")
    assert block.raw.startswith("x" * 100)
    assert block.raw.endswith("…[truncated]")


def test_tool_call_raw_is_compact_json():
    result = parse_agent_log(J(type="assistant", message={"content": [
        {"type": "tool_use", "id": "t", "name": "Bash",
         "input": {"command": "echo hi", "description": "say hi"}}]}))
    assert result.blocks[0].raw == \
        '{"command": "echo hi", "description": "say hi"}'


# ── block cap and scale ─────────────────────────────────────────────────

def test_block_cap_drops_front_and_flags_truncation():
    lines = [J(type="assistant", message={"content": [
        {"type": "text", "text": f"msg {i}"}]}) for i in range(15)]
    result = parse_agent_log(logtext(*lines), max_blocks=10)
    assert len(result.blocks) == 10
    assert result.truncated is True
    assert result.blocks[0].text == "msg 5"    # oldest 5 dropped
    assert result.blocks[-1].text == "msg 14"


def test_large_input_parses_without_error():
    lines = []
    for i in range(2500):
        lines.append(J(type="assistant", message={"content": [
            {"type": "text", "text": f"msg {i}"}]}))
        lines.append(f"plain line {i}")
    result = parse_agent_log(logtext(*lines))
    assert len(result.blocks) == 5000
    assert result.truncated is False


# ── to_dict shape ───────────────────────────────────────────────────────

def test_to_dict_omits_unset_fields():
    d = LogBlock(kind="assistant_text", text="hi").to_dict()
    assert d == {"kind": "assistant_text", "text": "hi"}
    d = LogBlock(kind="tool_call", tool_name="Bash",
                 tool_id="t", tool_summary="ls").to_dict()
    assert d == {"kind": "tool_call", "tool_name": "Bash",
                 "tool_id": "t", "tool_summary": "ls"}
    d = LogBlock(kind="result_text", text="x", error=True).to_dict()
    assert d == {"kind": "result_text", "text": "x", "error": True}
