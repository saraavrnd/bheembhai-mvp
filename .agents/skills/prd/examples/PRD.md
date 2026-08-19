# PRD — Learn Portal

**Status:** Draft · **Owner:** Product · **Last updated:** 2026-06-19

## 1. Problem & Opportunity
K-12 classrooms run at a single pace, so students who hit a conceptual roadblock fall behind
and compounding gaps form. Students often cannot describe their confusion in academic terms,
which makes it hard for static materials or busy teachers to intervene in time. There is an
opportunity to give every student an always-available tutor that diagnoses confusion from
plain language and guides them to understanding without simply handing over answers.

## 2. Target Users
| User type | Primary need | Notes |
|-----------|--------------|-------|
| Student (learner) | Get unstuck on a concept at their own pace, in plain language | K-12; reading level and accessibility matter |
| Teacher (orchestrator) | See who is struggling and on what, without reading every chat | Wants signals, not surveillance |
| District admin | Adopt safely and compliantly; justify the spend | Owns procurement and data governance |

## 3. Goals & Success Metrics
- G1 — Students resolve conceptual roadblocks independently via guided dialogue.
  - Metric: 35% improvement in quiz scores for active users.
- G2 — Teachers spend less time on reactive one-on-one support.
  - Metric: 4-6 hours saved per teacher per week.
- G3 — Early intervention on at-risk students.
  - Metric: at-risk flag raised up to 2 weeks before traditional assessment.

## 4. Scope
**In scope (MVP):** Socratic chat tutor grounded in teacher materials; document ingestion;
core safety guardrails; basic teacher dashboard signals; local deployment.
**Out of scope (Later):** cross-school data normalisation, advanced role inheritance,
automated administrative workflows, full predictive at-risk automation.

## 5. Feature Areas
| Area code | Feature area | One-line description |
|-----------|--------------|----------------------|
| FA-1 | Socratic dialogue engine | Guided, no-direct-answer tutoring with scaffolding |
| FA-2 | Curriculum knowledge base | Ingest teacher materials and retrieve grounded context |
| FA-3 | Visual & diagram learning | Generate diagrams and interpret student sketches |
| FA-4 | Answer grounding & accuracy | Verify responses against sources; route hard math |
| FA-5 | Teacher dashboard | Surface session-level signals and at-risk flags |
| FA-6 | SIS integration | Import rosters reliably and idempotently |
| FA-7 | Safety, privacy & compliance | Guardrails, PII handling, regulatory controls |
| FA-8 | Deployment & operations | Reproducible local Docker deployment |

## 6. Functional Requirements
| ID | Feature area | Requirement (one testable capability) | Release |
|------|--------------|----------------------------------------|---------|
| FR-001 | FA-1 | Student can ask a question in informal natural language and receive a guided prompt rather than a direct answer | MVP |
| FR-002 | FA-1 | System offers scaffolding cards that break a task into steps (e.g. select a hypothesis, identify a formula) | MVP |
| FR-003 | FA-1 | System never reveals the final answer even when asked directly; it continues to guide | MVP |
| FR-004 | FA-2 | Teacher can upload course materials (PDF, DOCX, TXT) for a subject | MVP |
| FR-005 | FA-2 | System chunks uploaded materials and makes them retrievable for grounding | MVP |
| FR-006 | FA-2 | System retrieves the most relevant material to ground each tutoring response | MVP |
| FR-007 | FA-3 | System generates a flow/structure diagram from a student's relational question | MVP |
| FR-008 | FA-3 | System recovers automatically from a diagram rendering failure without breaking the session | MVP |
| FR-009 | FA-3 | System interprets a student-uploaded diagram image and annotates it with feedback | Later |
| FR-010 | FA-4 | System checks each response against retrieved sources and regenerates unverified claims | MVP |
| FR-011 | FA-4 | System routes symbolic math to a specialised solver and returns it as a guided prompt | Later |
| FR-012 | FA-5 | Teacher dashboard shows session-level signals (strategy types, time-on-task, recall) without raw chat logs | MVP |
| FR-013 | FA-5 | Dashboard flags at-risk students ahead of assessments | Later |
| FR-014 | FA-6 | System imports a student roster via OneRoster or CSV | MVP |
| FR-015 | FA-6 | System rejects malformed roster rows while processing valid ones, and logs each rejection | MVP |
| FR-016 | FA-6 | Re-importing the same roster does not create duplicate student profiles | MVP |
| FR-017 | FA-7 | A safety classifier blocks unsafe inputs/outputs while allowing valid academic terms | MVP |
| FR-018 | FA-7 | A student or teacher can request deletion of their data and have it removed | MVP |

## 7. Non-Functional Requirements
| ID | Category | Requirement | Release |
|------|----------|-------------|---------|
| NFR-001 | Privacy | Retrieved context has PII masked and instruction-like formatting stripped before reaching the model | MVP |
| NFR-002 | Security | Student profiles are encrypted at rest with role-based access | MVP |
| NFR-003 | Compliance | System meets FERPA, COPPA, SOPIPA, and GDPR obligations applicable to K-12 | MVP |
| NFR-004 | Accessibility | Student UI meets WCAG AA and age-appropriate reading levels | MVP |
| NFR-005 | Operability | Entire system deploys reproducibly via docker-compose on a local environment | MVP |

## 8. Constraints & Assumptions
Deployment is local Docker for now, designed to extend to remote/cloud later. Development uses
Jira (issues via Atlassian MCP) and GitHub. Assumption: teachers provide their own curriculum
materials; the system does not supply curriculum content.

## 9. Open Questions
- Which subjects are in the pilot (affects ingestion testing)?
- What is the district's data-retention period for deleted records?
- Is the at-risk flag in MVP or deferred? (Currently marked Later.)

---
*Generated by the `prd` skill. Next step: run `prd-decompose` to turn feature areas into Jira epics.*
