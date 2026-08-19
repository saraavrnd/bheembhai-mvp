FROM node:20-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      git python3 python3-pip ca-certificates jq curl && rm -rf /var/lib/apt/lists/*

# uv / uvx — required by the Atlassian MCP (`uvx mcp-atlassian`). node:20-slim has npx
# (for the GitHub MCP) but NOT uvx, so install uv here.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    cp /root/.local/bin/uv /root/.local/bin/uvx /usr/local/bin/ && \
    chmod 755 /usr/local/bin/uv /usr/local/bin/uvx

ARG CLAUDE_VERSION=latest
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_VERSION} || \
    echo "NOTE: install claude-code manually if this fails"

# MCP server config template. Credentials are injected at runtime via env (never baked in).
COPY mcp.json /opt/mcp.json
COPY run_skill.sh /usr/local/bin/run_skill
# PURE RUNTIME — no skills baked in (Phase 1). Each step's ONE skill is
# downloaded from its pinned S3 bundle (BB_SKILL_URL) by run_skill.sh and
# extracted over .claude/skills/ in the worktree; BheemBhai is authoritative.
# The container runs as non-root, so everything it must READ needs world-readable perms.
# (COPY defaults to root ownership with restrictive perms — this bit us: jq could not open
# /opt/mcp.json as `node`, so no MCP config was ever written.)
RUN chmod +x /usr/local/bin/run_skill && \
    chmod 644 /opt/mcp.json

# Run as NON-ROOT. Claude Code refuses --dangerously-skip-permissions under root, and the
# agent must run unattended (no one is there to approve Bash/MCP tool calls in a container).
# The base image already ships a `node` user (uid 1000).
RUN mkdir -p /workspace /out /home/node/.claude && \
    chown -R node:node /workspace /out /home/node
USER node
ENV HOME=/home/node
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/run_skill"]
