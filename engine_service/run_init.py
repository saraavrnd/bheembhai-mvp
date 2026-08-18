"""Run initialization — ADR-013 §2 (engine-owned branch, model resolution, step rows).

On claiming a `start` work item, the engine executes an idempotent init sequence
BEFORE any task launch. Failure is classified (`failed_execution` = deterministic,
bad input/auth; `failed_infra` = transient, retryable) and surfaces as a run-level
failure with the reason in `transitions` — no container minutes are ever spent on
an uninitialisable run.

Idempotency (ADR-003 crash re-claim): everything here is safe to re-run. If the
run already carries `run_branch` (a prior init committed before a crash), branch
creation is skipped and step rows are not re-inserted.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bheembhai.github import (
    DEFAULT_GITHUB_API,
    DEFAULT_GITHUB_URL,
    GITHUB_API_HEADERS,
    GitTarget,
    _api_base_from_host,
    _clone_base,
    _slug_from_url,
)
from bheembhai.models.project import ProjectIntegration
from bheembhai.models.run import Run, Step
from bheembhai.models.workflow import Policy, Workflow
from bheembhai.resolver import ResolvedIntegration, mask_credential, resolve_credentials

from engine_service.persistence import record_transition
from engine_service.skills import (
    effective_skill_map,
    load_run_skills,
    materialize_skills,
)
from engine_service.workflow import (
    PolicySpec,
    TierResolutionError,
    WorkflowSpec,
    allowed_models,
    resolve_model_tier,
    validate_pairing,
    validate_workflow,
)

logger = logging.getLogger(__name__)


class InitFailure(Exception):
    """Run init failed with a classified kind. `kind` is one of the run-level
    failure statuses (`failed_execution` / `failed_infra`) — the dispatcher maps
    it onto the run without launching anything."""

    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


@dataclass
class InitContext:
    """Everything the state machine needs after init — one object, no re-queries."""
    run: Run
    workflow_spec: WorkflowSpec
    policy_spec: PolicySpec
    github: ResolvedIntegration
    ai_vendor: ResolvedIntegration
    jira: ResolvedIntegration | None
    git_target: GitTarget
    source_branch: str
    run_branch: str
    model_map: dict[str, str]      # step_id -> resolved concrete vendor model id
    skills_overlay: bool = False   # True → /skills bind mount + BB_SKILLS_DIR env


# ── Branch name derivation ──────────────────────────────────────────────

def safe_story(story: str) -> str:
    """Fold a Jira key into a git-branch-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(story or "").lower()).strip("-")
    return slug or "story"


def derive_run_branch(story_id: str | None, run_id, *, now: datetime | None = None) -> str:
    """`feat/<safe_story>/<DDMMYYYYHHmm>-<first-4-of-run-uuid>` (ADR-013 §2 step 3).
    The uuid suffix makes collisions across same-minute runs impossible."""
    story = safe_story(story_id or "story")
    stamp = (now or datetime.now()).strftime("%d%m%Y%H%M")
    suffix = str(run_id).replace("-", "")[:4]
    return f"feat/{story}/{stamp}-{suffix}"


# ── GitHub coordinates ──────────────────────────────────────────────────
# (normalization helpers live in bheembhai.github — shared with the platform)

def compose_git_target(config: dict) -> GitTarget:
    """(api_base, clone_url, repo slug) from a GitHub integration config.

    Pinned normalization:
    - `repository` may be "owner/repo", a full https URL, or an ssh URL.
    - api base: github.com → api.github.com; explicit api host kept; anything
      else treated as GitHub Enterprise ({host}/api/v3).
    - clone url: a full URL in `repository` is used verbatim; otherwise
      {browser base}/{repository} with the `.git` suffix guaranteed.
    - Missing/malformed repository raises InitFailure(failed_execution) — a
      config problem, not a transient one.
    """
    repo = str(config.get("repository") or "").strip()
    if not repo:
        raise InitFailure(
            "failed_execution",
            "GitHub integration has no 'repository' config value (expected owner/repo)")

    base = str(config.get("url") or DEFAULT_GITHUB_URL).strip().rstrip("/")

    if repo.startswith(("http://", "https://", "ssh://", "git@")):
        slug = _slug_from_url(repo)
        if "/" not in slug:
            raise InitFailure(
                "failed_execution",
                f"GitHub 'repository' URL {repo!r} does not end in owner/repo")
        return GitTarget(api_base=_api_base_from_host(base), clone_url=repo, repository=slug)

    if "/" not in repo:
        raise InitFailure(
            "failed_execution",
            f"GitHub 'repository' must be 'owner/repo', got {repo!r}")

    clone = f"{_clone_base(config)}/{repo}"
    if not clone.endswith(".git"):
        clone += ".git"
    return GitTarget(api_base=_api_base_from_host(base), clone_url=clone, repository=repo)


# ── Branch creation via the GitHub REST API ────────────────────────────

def _gh_classify(status: int, detail: str) -> InitFailure:
    """ADR-013 §2 step 4 classification: auth/not-found → failed_execution
    (deterministic — retrying changes nothing); 5xx → failed_infra (transient)."""
    if status in (401, 403, 404):
        return InitFailure("failed_execution", f"GitHub returned HTTP {status}: {detail}")
    if 400 <= status < 500:
        return InitFailure("failed_execution", f"GitHub returned HTTP {status}: {detail}")
    return InitFailure("failed_infra", f"GitHub returned HTTP {status}: {detail}")


async def _get_ref_sha(client: httpx.AsyncClient, api_base: str, token: str,
                       repository: str, branch: str) -> str:
    """HEAD sha of a branch, or raise a classified InitFailure."""
    url = f"{api_base}/repos/{repository}/git/ref/heads/{branch}"
    try:
        resp = await client.get(
            url, headers={**GITHUB_API_HEADERS, "Authorization": f"Bearer {token}"})
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
        raise InitFailure("failed_infra", f"GitHub unreachable ({exc.__class__.__name__})") from exc
    if resp.status_code != 200:
        raise _gh_classify(resp.status_code, (resp.text or "")[:200])
    return resp.json()["object"]["sha"]


async def _create_ref(client: httpx.AsyncClient, api_base: str, token: str,
                      repository: str, branch: str, sha: str) -> httpx.Response:
    """POST git/refs — the caller inspects the status (201 = created, 422 = exists)."""
    url = f"{api_base}/repos/{repository}/git/refs"
    try:
        return await client.post(
            url,
            headers={**GITHUB_API_HEADERS, "Authorization": f"Bearer {token}"},
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
        raise InitFailure("failed_infra", f"GitHub unreachable ({exc.__class__.__name__})") from exc


async def create_branch_github(git_target: GitTarget, token: str,
                               source_branch: str, run_branch: str,
                               *, client: httpx.AsyncClient | None = None,
                               timeout: float = 15.0) -> str:
    """Create `run_branch` at the source branch HEAD (ADR-013 §2 step 4).

    Idempotent: a ref that already exists at the same sha proceeds (a prior init
    already happened); a ref at a different sha suffix-bumps (`-2`) and retries
    once. Returns the branch name actually created.

    Classification: 401/403/404 → failed_execution; 5xx/network → failed_infra.
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        sha = await _get_ref_sha(client, git_target.api_base, token,
                                 git_target.repository, source_branch)

        for name in (run_branch, f"{run_branch}-2"):
            resp = await _create_ref(client, git_target.api_base, token,
                                     git_target.repository, name, sha)
            if resp.status_code == 201:
                logger.info("branch %s created at %s (sha %.7s…)",
                            name, source_branch, sha)
                return name
            if resp.status_code != 422:
                raise _gh_classify(resp.status_code, (resp.text or "")[:200])

            # 422: ref already exists — OR a validation failure (bad sha,
            # protected branch). Fetch the ref to tell which.
            try:
                existing = await _get_ref_sha(client, git_target.api_base, token,
                                              git_target.repository, name)
            except InitFailure:
                raise InitFailure(
                    "failed_execution",
                    f"branch creation refused for {name}: {(resp.text or '')[:200]}") from None
            if existing == sha:
                logger.info("branch %s already exists at the source sha — init idempotent", name)
                return name
            logger.warning("branch %s exists at a different sha — suffix bump", name)

        raise InitFailure(
            "failed_execution",
            f"branch {run_branch} and {run_branch}-2 both exist at different shas — "
            "manual cleanup required")
    finally:
        if own_client:
            await client.aclose()


# ── The init sequence ───────────────────────────────────────────────────

def _pick(resolved: list[ResolvedIntegration], integration_id) -> ResolvedIntegration | None:
    for r in resolved:
        if r.integration_id == str(integration_id):
            return r
    return None


async def init_run(session: AsyncSession, run_id, config, secure_storage) -> InitContext:
    """ADR-013 §2: load → validate → resolve models → create branch → persist.

    All-or-nothing: everything is staged on `session` and committed once at the
    end. On InitFailure nothing is committed (the caller records the failure).
    """
    run = await session.get(Run, run_id)
    if run is None:
        raise InitFailure("failed_execution", f"run {run_id} not found")

    workflow = await session.get(Workflow, run.workflow_id)
    policy = await session.get(Policy, run.policy_id)
    if workflow is None or policy is None:
        raise InitFailure("failed_execution", "run references a missing workflow or policy")

    wf_spec = WorkflowSpec.load_yaml(workflow.yaml_content)
    pol_spec = PolicySpec.load_yaml(policy.yaml_content)

    # ── Integrations: GitHub + AI vendor required, Jira optional at MVP ──
    gh_row = (await session.get(ProjectIntegration, run.github_integration_id)
              if run.github_integration_id else None)
    vendor_row = (await session.get(ProjectIntegration, run.ai_vendor_integration_id)
                  if run.ai_vendor_integration_id else None)
    jira_row = (await session.get(ProjectIntegration, run.jira_integration_id)
                if run.jira_integration_id else None)
    if gh_row is None or vendor_row is None:
        raise InitFailure(
            "failed_execution",
            "run requires both a GitHub and an AI-vendor integration selection")

    resolved = await resolve_credentials(
        [i for i in (gh_row, vendor_row, jira_row) if i], secure_storage)
    github = _pick(resolved, run.github_integration_id)
    ai_vendor = _pick(resolved, run.ai_vendor_integration_id)
    jira = _pick(resolved, run.jira_integration_id)
    if github is None:
        raise InitFailure(
            "failed_execution",
            f"GitHub credential not resolvable at ref '{gh_row.credential_ref}' "
            "(fingerprint unavailable — credential missing in Secure Storage)")
    if ai_vendor is None:
        raise InitFailure(
            "failed_execution",
            f"AI-vendor credential not resolvable at ref '{vendor_row.credential_ref}'")
    logger.info("run %s: credentials resolved — github …%s, ai-vendor(%s) …%s%s",
                run_id, mask_credential(github.token),
                ai_vendor.type, mask_credential(ai_vendor.token),
                f", jira …{mask_credential(jira.token)}" if jira else "")

    # ── Validate workflow/policy pairing + skill existence ──
    # Project skills shadow platform skills by name; the effective set is what
    # the run may reference and what gets materialized to /skills below.
    project_skills, platform_skills = await load_run_skills(session, run.project_id)
    effective_skills = effective_skill_map(project_skills, platform_skills)
    known_skills = set(effective_skills)
    validate_workflow(wf_spec, known_skills=known_skills)
    validate_pairing(wf_spec, pol_spec)

    # ── Project skill overlay: materialize the FULL effective library ──
    # The /skills bind mount replaces the image's baked copy, so everything
    # the run may reference must be on disk at <workdir>/skills/<run_id>.
    # init re-runs on every dispatch claim, so PM edits apply at the next
    # dispatch without touching in-flight runs.
    skills_overlay = bool(project_skills)
    if skills_overlay:
        try:
            materialize_skills(config.engine.workdir, run_id, effective_skills)
        except OSError as exc:
            raise InitFailure(
                "failed_infra",
                f"skill library materialization failed: {exc}") from exc

    # ── Resolve models: tier → concrete id through the vendor's flat config ──
    model_map: dict[str, str] = {}
    for step_id, spec in wf_spec.steps.items():
        try:
            model_map[step_id] = resolve_model_tier(spec.get("model"), ai_vendor.config)
        except TierResolutionError as exc:
            raise InitFailure("failed_execution", f"step '{step_id}': {exc}") from exc
    allowed = allowed_models(ai_vendor.config)
    if not allowed:
        raise InitFailure(
            "failed_execution",
            f"AI-vendor integration '{ai_vendor.label}' maps no model tiers "
            "(model_high/medium/low config keys are empty)")

    git_target = compose_git_target(github.config)
    # The run row wins: the platform resolves the user's per-run override (or
    # the integration's base_branch at submit time) into runs.source_branch.
    # The live integration config is only a fallback for runs predating the
    # override (or rows filled by older platform versions).
    source_branch = str(run.source_branch or github.config.get("base_branch") or "main")

    # ── Branch: derive + create unless a prior init already did (idempotent) ──
    first_init = run.state == "pending"
    run.source_branch = source_branch
    if run.run_branch:
        run_branch = run.run_branch
        logger.info("run %s already carries branch %s — init resumed, creation skipped",
                    run_id, run_branch)
    else:
        run_branch = await create_branch_github(
            git_target, github.token, source_branch,
            derive_run_branch(run.story_id, run_id))
        run.run_branch = run_branch

    # ── Persist: run state, step rows (once), transitions ──
    if first_init:
        existing_steps = (
            await session.execute(select(Step).where(Step.run_id == run_id))
        ).scalars().first()
        if existing_steps is None:
            for step_id, spec in wf_spec.steps.items():
                session.add(Step(
                    run_id=run_id,
                    step_id=step_id,
                    skill=spec.get("skill", step_id),
                    model_requested=model_map.get(step_id),
                ))
        record_transition(session, run_id, "pending", "running",
                          reason=f"branch created: {run_branch} (from {source_branch})")
        record_transition(session, run_id, "pending", "running",
                          reason="models resolved: " +
                                 ", ".join(f"{sid}={mid}" for sid, mid in model_map.items()))
        run.state = "running"

    await session.commit()
    logger.info("run %s: init complete — branch=%s models=%s",
                run_id, run_branch, model_map)

    return InitContext(
        run=run,
        workflow_spec=wf_spec,
        policy_spec=pol_spec,
        github=github,
        ai_vendor=ai_vendor,
        jira=jira,
        git_target=git_target,
        source_branch=source_branch,
        run_branch=run_branch,
        model_map=model_map,
        skills_overlay=skills_overlay,
    )
