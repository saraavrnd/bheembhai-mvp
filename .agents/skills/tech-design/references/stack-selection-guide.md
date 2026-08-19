# Stack Selection Guide (propose → approve)

This skill recommends a stack; the user approves it. Follow these rules so the recommendation
is honest and the approval gate is real.

## The approval gate is mandatory
- Always present options before recommending; never write the committed design until the user
  approves.
- If the user changes a choice, revise the proposal and re-confirm — don't silently proceed.
- Approval can be partial ("yes to stack, still deciding on DB"); lock what's approved, keep
  the rest open.

## How to choose (in priority order)
1. **Constraints first.** Deployment target, team skills, compliance, existing infra, budget.
   A "better" technology the team can't run or deploy is the wrong choice.
2. **Fit to the requirements.** Pick for what the PRD actually needs, not for novelty. If the
   MVP is CRUD + a chat flow, it does not need a microservice mesh.
3. **Right-size for the MVP.** Default to the simplest architecture that meets the NFRs —
   usually a modular monolith with clear internal boundaries. Name the seams where it would
   split later instead of splitting now.
4. **Boring where it doesn't matter, sharp where it does.** Use well-trodden defaults for the
   plumbing; spend the novelty budget only on the parts that are core to the product.
5. **Maturity & longevity.** Prefer libraries with active maintenance and stable APIs. Flag
   anything pre-1.0 or recently breaking.

## Always give honest trade-offs
For each decision, list 2-3 real options with genuine pros and cons (not a strawman next to
the favourite). State why the recommended one wins given THIS product's constraints.

## Pin current versions
Recommend specific, current stable versions. If web search is available, verify the latest
stable release and check for recent breaking changes rather than relying on training data —
versions move. Record pinned versions in `tech-stack.md`.

## Make NFRs concrete here
Translate each NFR into a stack/design choice: "encrypted at rest" → which DB feature / KMS;
"WCAG AA" → which component approach; "deploys locally" → docker-compose. An NFR with no
mechanism is unbuilt.

## Default testing approach: TDD
Unless the user objects, propose TDD: acceptance criteria become failing tests before
implementation. Name a test runner per layer (unit, integration, e2e). This is what
`test-creator` and `implement` rely on downstream, so make it explicit in `testing-strategy.md`.
