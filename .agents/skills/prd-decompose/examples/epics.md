# Epics — decomposed from Learn Portal PRD

Project: **LEARN** · Source: `PRD.md` · 8 epics · every FR/NFR mapped exactly once.

> Keys shown as LEARN-NN are illustrative. When run with the Atlassian MCP connected, the
> skill creates these and records the real returned keys in `epic-map.json`.

---

## EPIC: Socratic dialogue engine  (LEARN-10)
**Feature area:** FA-1 · **Release:** MVP · **Labels:** FA-1, MVP
**Description:** The core tutoring experience for the learner-facing product. This epic builds the
conversation loop that keeps the assistant in tutoring mode: it should guide students through
incremental prompts, ask clarifying questions, and avoid simply revealing the answer.
**Goal / Definition of done:** A student can work through a concept in natural language and be
guided toward the answer without the system giving away the solution, while the tutor maintains
the current dialogue state across turns.
**Functional scope**
- Maintain a tutoring conversation state for the active session.
- Generate scaffolded follow-up prompts that adapt to the student's response.
- Keep the conversation aligned to the intended learning objective and not drift into answer dumping.
**Implementation guidance / dependency order**
- Start with the conversation state model and response contract so the rest of the flow has a stable
  backbone.
- Add the tutoring policy / prompt scaffolding rules next.
- Integrate the engine with curriculum context once the basic dialogue loop is working.
**Covered requirements:** FR-001, FR-002, FR-003

## EPIC: Curriculum knowledge base  (LEARN-11)
**Feature area:** FA-2 · **Release:** MVP · **Labels:** FA-2, MVP
**Description:** The content ingestion and retrieval layer that grounds tutoring in the actual
curriculum. This epic lets teachers provide source material, turns that material into retrievable
knowledge, and makes the right passages available to the dialogue engine.
**Goal / Definition of done:** A teacher can upload materials and the tutor can ground its guidance
in retrieved passages from those materials instead of relying only on generic model knowledge.
**Functional scope**
- Accept teacher-provided curriculum content.
- Normalize, chunk, and index source materials for retrieval.
- Return relevant passages and citations for use in tutoring responses.
**Implementation guidance / dependency order**
- Build the storage model and ingestion pipeline first.
- Add indexing and retrieval after the content model is stable.
- Expose retrieval APIs to the dialogue engine only after the grounding path is testable end-to-end.
**Covered requirements:** FR-004, FR-005, FR-006

## EPIC: Visual & diagram learning  (LEARN-12)
**Feature area:** FA-3 · **Release:** mixed · **Labels:** FA-3, mixed
**Description:** Diagram support for questions that are easier to understand visually. MVP covers
diagram generation from the tutor; the later part extends the system to interpret and annotate
student-uploaded sketches.
**Goal / Definition of done:** The tutor can generate a valid diagram and recover cleanly from
rendering errors; sketch interpretation remains deferred to the later slice.
**Functional scope**
- Generate diagrams from student questions and learning context.
- Render diagrams reliably and handle invalid output gracefully.
- Later: accept student sketches and annotate or interpret them.
**Implementation guidance / dependency order**
- Implement the diagram serialization / rendering path first so the output format is stable.
- Add validation and failure recovery around the renderer before broadening to more diagram types.
- Keep sketch interpretation as a separate later story stream because it depends on the base diagram pipeline.
**Covered requirements:** FR-007, FR-008, FR-009
**MVP subset:** FR-007, FR-008  (FR-009 is Later)

## EPIC: Answer grounding & accuracy  (LEARN-13)
**Feature area:** FA-4 · **Release:** mixed · **Labels:** FA-4, mixed
**Description:** Guardrails that keep answers truthful and mathematically correct. This epic
verifies response claims against source material and routes symbolic math to a specialised solver
instead of guessing.
**Goal / Definition of done:** The system can detect unsupported claims and regenerate them, and it
can route supported math problems through the dedicated solver path.
**Functional scope**
- Verify generated answers against available source material.
- Flag unsupported claims and trigger regeneration or correction.
- Route hard symbolic math to a specialised solver integration.
**Implementation guidance / dependency order**
- Build the source-verification path before the math solver, because factual grounding is the
  higher-priority safety rail.
- Add the solver adapter as a separate integration step once verification is stable.
- Define the retry / regeneration policy early so downstream stories have clear failure handling.
**Covered requirements:** FR-010, FR-011
**MVP subset:** FR-010  (FR-011 is Later)

## EPIC: Teacher dashboard  (LEARN-14)
**Feature area:** FA-5 · **Release:** mixed · **Labels:** FA-5, mixed
**Description:** A teacher-facing view of learner progress that surfaces session-level signals rather
than raw chat logs. The point is to show whether a student is progressing, struggling, or needs
attention without exposing unnecessary conversation detail.
**Goal / Definition of done:** Teachers can see session summaries and instructional signals, with the
at-risk flagging flow added in the later slice.
**Functional scope**
- Show session-level progress and strategy signals.
- Highlight time spent, recall strength, and similar progress markers.
- Later: surface at-risk or intervention flags.
**Implementation guidance / dependency order**
- Start with data capture from the run / session events so the dashboard has real inputs.
- Add aggregate metrics and summary cards before more advanced risk logic.
- Build the teacher UI on top of those aggregates, then layer in alerting or at-risk heuristics later.
**Covered requirements:** FR-012, FR-013
**MVP subset:** FR-012  (FR-013 is Later)

## EPIC: SIS integration  (LEARN-15)
**Feature area:** FA-6 · **Release:** MVP · **Labels:** FA-6, MVP
**Description:** Secure roster ingestion from school systems. This epic handles the first external
system integration by importing student and class data from OneRoster or CSV and keeping the import
process auditable and repeatable.
**Goal / Definition of done:** A roster imports cleanly, bad rows are logged, and re-imports do not
duplicate students or classes.
**Functional scope**
- Import roster data from OneRoster or CSV sources.
- Validate incoming rows and record row-level errors.
- Make imports idempotent so the same feed can be processed again safely.
**Implementation guidance / dependency order**
- Define the provider / source mapping and canonical roster model first.
- Add validation and error reporting before automatic upsert behavior.
- Finish with idempotent import logic and re-run handling so support can safely repeat imports.
**Covered requirements:** FR-014, FR-015, FR-016

## EPIC: Safety, privacy & compliance  (LEARN-16)
**Feature area:** FA-7 + cross-cutting NFRs · **Release:** MVP · **Labels:** FA-7, MVP
**Description:** The platform's protection layer for student data and risky content. This epic gathers
safety classification, PII handling, encryption/access control, deletion rights, and compliance
concerns so they are implemented as one coherent policy surface instead of scattered rules.
**Goal / Definition of done:** Unsafe content is blocked, PII is protected, deletion works, and the
expected compliance obligations are met.
**Functional scope**
- Classify unsafe or policy-violating content.
- Protect and minimize PII exposure.
- Support deletion and compliance-oriented retention behavior.
- Keep security controls consistent across the platform.
**Implementation guidance / dependency order**
- Establish the content policy and data-handling rules first, because they influence the rest of the stack.
- Add enforcement hooks in the core flows before polishing UI surfaces.
- Implement deletion / retention behavior after the storage and audit model is stable.
**Covered requirements:** FR-017, FR-018, NFR-001, NFR-002, NFR-003, NFR-004

## EPIC: Deployment & operations  (LEARN-17)
**Feature area:** FA-8 · **Release:** MVP · **Labels:** FA-8, MVP
**Description:** Reproducible local deployment and operational readiness for the full stack. This
epic makes it easy to bring the system up on a clean machine and gives the team a reliable baseline
for development, testing, and handoff.
**Goal / Definition of done:** The system comes up via `docker-compose` on a clean machine with the
main services available in the expected order.
**Functional scope**
- Define the local deployment topology.
- Wire the runtime services together with predictable configuration.
- Add basic readiness / health behavior for the core stack.
**Implementation guidance / dependency order**
- Start with the base container and environment setup so the service graph is reproducible.
- Add health checks and startup ordering once the containers are stable.
- Finish with documentation and operator notes so developers can run the stack confidently.
**Covered requirements:** NFR-005

---
*Generated by `prd-decompose`. Next step: run `user-story` on each epic. Coverage verified:
all 18 FRs and 5 NFRs are mapped to exactly one epic.*
