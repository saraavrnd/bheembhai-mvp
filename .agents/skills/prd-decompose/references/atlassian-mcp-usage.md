# Driving the Atlassian (Jira) MCP

These skills create issues through the **connected Atlassian MCP**, not via a REST script.
Tool names vary slightly by MCP version, so discover them rather than hard-coding. The
patterns below describe the *intent* of each call; use whatever the connected server exposes.

## 1. Before creating anything — discover context
- Find the available Jira tools (search/list issues, create issue, get project, etc.).
- **List or search accessible projects** and confirm the target **project key** with the user
  (e.g. LEARN). Never guess the key.
- **Confirm the issue type names** in that project. "Epic" and "Story" are common defaults but
  projects can rename them or restrict which types are allowed. If unsure, fetch the project's
  create metadata / issue types.

## 2. Creating an Epic
Provide at minimum:
- project key
- issue type = Epic (or the project's epic-equivalent type)
- summary (the epic's outcome-focused title)
- description (paste the rendered epic body from epic.md.tmpl)
- labels (feature-area code + release, e.g. `FA-3`, `MVP`)

Capture the **returned issue key** (e.g. LEARN-12) — you need it for the traceability map and
for parenting stories.

When writing the epic description, make it self-contained:
- State the domain problem in plain language.
- Name the primary users or actors involved.
- Describe the concrete functionality and workflows the epic covers.
- Note the implementation order when there is a natural dependency chain.
- Mention any prerequisite models, integrations, or platform services that must exist first.
- Keep the text understandable to a Jira reader who has not seen the PRD.

## 3. Creating a Story under an Epic
Provide:
- project key
- issue type = Story
- summary
- description (user-story statement + context)
- acceptance criteria — put these in the description (Gherkin/checklist) AND/OR the dedicated
  acceptance-criteria field if the project has one
- **parent / epic link** — link the story to its epic key so the hierarchy is correct. The
  exact field depends on the project (team-managed projects use a parent field; company-managed
  classic projects use an "Epic Link"). If the first attempt is rejected, fetch the create
  metadata to find the right field and retry.
- labels, story points (if the field exists and an estimate is known)

## 4. Safety & sequencing rules
- Create issues **one at a time** and verify each succeeds before the next. Do not fire a
  blind batch — a mid-batch failure leaves a messy project.
- After each create, **record the returned key** immediately (in epic-map.json or the story
  output) so a crash doesn't lose traceability.
- If a call fails (auth, permission, wrong field), **stop and report** the exact error and
  which item failed. Let the user fix access rather than silently skipping.
- Be idempotent-minded: before bulk-creating, check whether issues with the same summaries
  already exist (search first) so re-running the skill doesn't duplicate the backlog.

## 5. If the MCP is unavailable
Fall back to a Jira CSV import file and tell the user. Do not block the whole chain on Jira
connectivity — the artifacts (epics.md, stories) are still produced for later import.

## Token-cost discipline (MCP results persist in context)
Every MCP result stays in context for the whole session, so Jira calls accumulate fast (this is a
top usage driver). Keep the footprint small:
- **Minimal fields:** request only key, summary, status, and links — never full descriptions,
  comments, or changelogs unless a step truly needs them.
- **Batch reads:** one query returning many issues beats N single-issue fetches. Fetch the epic's
  stories once, not story-by-story.
- **Don't re-fetch:** reuse what's already in context; don't poll the same issue repeatedly.
- **Write concisely:** after creating/transitioning an issue, keep only the returned key + status;
  don't echo the full payload back into context.
- **Operator tips:** `/compact` between stories to flush accumulated tool results; disable MCP
  servers not needed for a given run. See `_SHARED-cost-and-models.md`.
