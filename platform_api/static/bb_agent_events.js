/* bb_agent_events.js — shared raw-event accordion renderer for agent.log
 * transcripts (ADR-011 logs endpoint, render=raw). Used by the ad-hoc
 * session page and the workflow results viewer.
 *
 * One collapsed <details> row per line of the transcript — the native left
 * arrow toggles the raw event (pretty-printed JSON, plain lines as-is). No
 * markdown: this is the machinery, not the conversation. Consecutive
 * thinking events collapse into ONE node whose summary sums them.
 *
 * Exposes: window.BB_AGENT_EVENTS = { maxLines: 400, render(text) }
 */
(function () {
    'use strict';

    function escHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    function stripAnsi(s) {
        return String(s || '').replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '');
    }

    // ── Thinking grouping ──
    // Two shapes feed the group: pure-thinking assistant messages +
    // thinking_delta stream events (text), and system/thinking_tokens events
    // (the newer Claude Code accounting stream — one line per token
    // increment, cumulative estimated_tokens).
    function thinkingChunk(ev) {
        if (!ev) return null;
        if (ev.type === 'system' && ev.subtype === 'thinking_tokens') {
            const tokens = ev.estimated_tokens != null ? Number(ev.estimated_tokens) : null;
            const delta = ev.estimated_tokens_delta != null ? Number(ev.estimated_tokens_delta) : null;
            if (tokens == null && delta == null) return null;
            return { kind: 'tokens', tokens: tokens, delta: delta };
        }
        if (ev.type === 'assistant' && ev.message && Array.isArray(ev.message.content)) {
            const blocks = ev.message.content;
            if (!blocks.length) return null;
            const parts = [];
            for (const b of blocks) {
                if (b && b.type === 'thinking') parts.push(String(b.thinking || ''));
                else if (b && b.type === 'redacted_thinking') parts.push('[redacted thinking]');
                else if (b && b.type === 'signature') parts.push('');
                else return null;   // mixed with text/tool_use — a regular event
            }
            const text = parts.join('\n').trim();
            return text ? { kind: 'text', text: text } : null;
        }
        if (ev.type === 'stream_event' && ev.event) {
            const e = ev.event;
            if (e.type === 'content_block_delta' && e.delta && e.delta.type === 'thinking_delta') {
                return { kind: 'text', text: String(e.delta.thinking || '') };
            }
            if (e.type === 'content_block_start' && e.content_block && e.content_block.type === 'thinking') {
                return { kind: 'text', text: String(e.content_block.thinking || '') };
            }
        }
        return null;
    }

    function tsOf(ev) {
        if (!ev || !ev.timestamp) return '';
        return escHtml(String(ev.timestamp).replace('T', ' ').slice(0, 19));
    }

    function summaryOf(line, ev) {
        if (!ev) return escHtml(stripAnsi(line).slice(0, 140));
        const ts = ' · ' + tsOf(ev);
        if (ev.type === 'assistant') {
            const blocks = (ev.message && ev.message.content) || [];
            const texts = blocks.filter(x => x && x.type === 'text').map(x => x.text).join(' ');
            const tool = blocks.find(x => x && x.type === 'tool_use');
            if (tool) return 'assistant · tool ' + escHtml(tool.name || '?') + ts;
            return 'assistant · ' + escHtml(stripAnsi(texts).slice(0, 120) || '(no text)') + ts;
        }
        if (ev.type === 'user') {
            const blocks = (ev.message && ev.message.content) || [];
            const texts = blocks
                .filter(x => x && x.type === 'tool_result')
                .map(x => String(x.content || '').replace(/\n/g, ' '))
                .join(' ');
            return 'user · ' + escHtml(stripAnsi(texts).slice(0, 120) || '(tool result)') + ts;
        }
        if (ev.type === 'result') {
            const bits = ['result'];
            if (ev.subtype) bits.push(escHtml(String(ev.subtype)));
            if (ev.is_error) bits.push('error');
            if (ev.num_turns != null) bits.push(ev.num_turns + ' turn' + (ev.num_turns === 1 ? '' : 's'));
            if (ev.total_cost_usd != null) bits.push('$' + Number(ev.total_cost_usd).toFixed(4));
            return bits.join(' · ') + ts;
        }
        if (ev.type === 'system') return 'system · ' + escHtml(String(ev.subtype || '')) + ts;
        if (ev.type === 'progress') return 'progress · ' + escHtml(String(ev.stage || '')) + ts;
        return escHtml(String(ev.type || 'event')) + ts;
    }

    function render(text) {
        const lines = String(text || '').split('\n').slice(-BB_AGENT_EVENTS.maxLines);
        if (!lines.length) return '<div class="log-plain">(empty log)</div>';
        let html = '';
        let think = [];   // open thinking group — flushed on the next non-thinking line

        function flushThink() {
            if (!think.length) return;
            const text = think.filter(t => t.kind === 'text')
                .map(t => t.text).join('\n').trim();
            const toks = think.filter(t => t.kind === 'tokens');
            let tokens = null;
            if (toks.length) {
                // estimated_tokens is cumulative — the last event of the
                // group is the total; sum deltas as a fallback for gaps.
                const last = toks[toks.length - 1];
                tokens = last.tokens != null ? last.tokens
                    : toks.reduce((a, t) => a + (t.delta || 0), 0);
            }
            const bits = ['<span class="tool-think" title="thinking">◈</span>',
                'thinking · ' + think.length + ' event' + (think.length === 1 ? '' : 's')];
            if (tokens != null) bits.push('~' + tokens.toLocaleString() + ' tokens');
            if (text) bits.push('~' + text.length.toLocaleString() + ' chars');
            if (think[0].ts) bits.push(think[0].ts);
            // Body: the reconstructed text stream (when there is one) plus
            // each token-accounting event raw, for audit.
            let body = text;
            if (toks.length) {
                body = [body, toks.map(t => t.raw).join('\n')].filter(Boolean).join('\n');
            }
            html += '<details class="raw-event raw-think"><summary>' + bits.join(' · ')
                + '</summary><pre>' + escHtml(stripAnsi(body)) + '</pre></details>';
            think = [];
        }

        for (const line of lines) {
            if (!line.trim()) continue;
            let ev = null;
            try { ev = JSON.parse(line); } catch (e) { /* plain line */ }
            const chunk = thinkingChunk(ev);
            if (chunk) {
                think.push({ kind: chunk.kind, text: chunk.text,
                             tokens: chunk.tokens, delta: chunk.delta,
                             raw: JSON.stringify(ev, null, 2), ts: tsOf(ev) });
                continue;
            }
            flushThink();
            const isObj = ev && typeof ev === 'object';
            const glyph = !isObj
                ? '<span class="tool-unk" title="plain line">·</span>'
                : ev.is_error
                    ? '<span class="tool-err" title="error">✗</span>'
                    : '<span class="tool-ok" title="ok">✓</span>';
            const body = isObj ? JSON.stringify(ev, null, 2) : stripAnsi(line);
            html += '<details class="raw-event"><summary>' + glyph + ' '
                + summaryOf(line, ev) + '</summary><pre>' + escHtml(body) + '</pre></details>';
        }
        flushThink();
        return html || '<div class="log-plain">(empty log)</div>';
    }

    window.BB_AGENT_EVENTS = { maxLines: 400, render: render };
})();
