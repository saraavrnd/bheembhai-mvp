"""Database engine and session factory — async SQLAlchemy 2.0 style."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bheembhai.config import DatabaseConfig
from bheembhai.models.base import Base

if TYPE_CHECKING:
    from bheembhai.models.workflow_category import WorkflowCategory

_logger = logging.getLogger(__name__)

_engine = None
_sessionmaker = None


def init_database(config: DatabaseConfig) -> None:
    """Initialise the async engine and session factory. Call once at startup."""
    global _engine, _sessionmaker
    _engine = create_async_engine(config.url, echo=config.echo)
    _sessionmaker = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


def get_sessionmaker():
    """Return the live session factory (None until ``init_database`` runs).

    ``from bheembhai.database import _sessionmaker`` snapshots the value at
    import time — which is None for modules imported before the app lifespan
    calls ``init_database``. Call this helper instead to resolve the current
    value each time.
    """
    return _sessionmaker


async def run_migrations() -> None:
    """Run pending Alembic migrations against the configured database.

    Replaces ``create_tables()`` as the startup schema-management step.
    Safe to call from multiple services simultaneously — concurrent runs
    are detected and silently ignored (the second caller sees the migration
    was already applied by its peer).
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.exc import ProgrammingError

    if _engine is None:
        raise RuntimeError("Database not initialised — call init_database first")

    # Resolve paths relative to *this file* (shared/bheembhai/database.py):
    #   shared_dir  = shared/
    #   alembic_ini = shared/alembic.ini
    #   scripts_dir = shared/alembic/
    shared_dir = Path(__file__).resolve().parent.parent
    alembic_ini = shared_dir / "alembic.ini"
    scripts_dir = shared_dir / "alembic"

    alembic_cfg = Config(str(alembic_ini))
    alembic_cfg.set_main_option("script_location", str(scripts_dir))
    alembic_cfg.set_main_option(
        "sqlalchemy.url",
        _engine.url.render_as_string(hide_password=False),
    )

    def _upgrade() -> None:
        try:
            command.upgrade(alembic_cfg, "head")
        except ProgrammingError:
            # A peer service (docker compose starts both at once) beat us to
            # the migration — the tables already exist.  Alembic recorded the
            # revision in that other transaction; our re-attempt would be a
            # no-op.  Let the caller continue.
            _logger.info(
                "Migrations already applied by a peer — continuing"
            )

    _logger.info("Running database migrations …")
    await asyncio.to_thread(_upgrade)
    _logger.info("Database migrations complete")


async def create_tables() -> None:
    """Create all ORM tables via ``Base.metadata.create_all`` (dev convenience).

    Prefer ``run_migrations()`` for production; this is kept for quick
    throwaway environments and test suites that don't need Alembic.
    """
    if _engine is None:
        raise RuntimeError("Database not initialised — call init_database first")
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def seed_default_roles() -> None:
    """Insert the default project roles if they don't already exist.

    Called at startup so FK constraints on memberships.role are always satisfied.
    """

    from bheembhai.models.user import ProjectRole

    defaults = [
        ProjectRole(key="project_manager", label="Project Manager", is_system_default=True),
        ProjectRole(key="developer", label="Developer", is_system_default=True),
        ProjectRole(key="qa", label="QA Engineer", is_system_default=True),
        ProjectRole(key="devops", label="DevOps Engineer", is_system_default=True),
        ProjectRole(key="ba", label="Business Analyst", is_system_default=True),
        ProjectRole(key="tech_lead", label="Tech Lead", is_system_default=True),
        ProjectRole(key="product_owner", label="Product Owner", is_system_default=True),
        ProjectRole(key="viewer", label="Viewer", is_system_default=True),
    ]

    async with _sessionmaker() as session:
        for role in defaults:
            existing = await session.get(ProjectRole, role.key)
            if existing is None:
                session.add(role)
        await session.commit()


async def seed_default_categories() -> None:
    """Insert the default workflow categories if they don't already exist.

    Called at startup (like ``seed_default_roles``) because categories are
    global reference data shared by platform workflows and their project
    copies. Idempotent get-or-create by case-insensitive name; existing rows
    (admin edits/renames) are never overwritten.
    """

    from sqlalchemy import func, select

    from bheembhai.models.workflow_category import WorkflowCategory

    defaults = [
        ("Software Delivery", "Feature, review, and release workflows for shipping code"),
        ("Operations", "Infrastructure, deployment, and incident-runbook workflows"),
        ("Marketing", "Campaign, content, and launch workflows"),
        ("Sales", "Deal, demo, and follow-up workflows"),
        ("Finance", "Reporting, reconciliation, and approval workflows"),
        ("Support", "Ticket triage, escalation, and SLA workflows"),
    ]

    async with _sessionmaker() as session:
        for name, description in defaults:
            existing = (
                await session.execute(
                    select(WorkflowCategory).where(
                        func.lower(WorkflowCategory.name) == name.lower()
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(WorkflowCategory(name=name, description=description))
        await session.commit()


async def _get_or_create_category(session: AsyncSession, name: str) -> WorkflowCategory | None:
    """Resolve a category by case-insensitive name, creating it on miss.

    Used by ``seed_default_workflows`` for the ``category:`` key in workflow
    YAML files. Returns ``None`` when the name is empty/unset.
    """

    from sqlalchemy import func, select

    from bheembhai.models.workflow_category import WorkflowCategory

    name = name.strip()
    if not name:
        return None

    existing = (
        await session.execute(
            select(WorkflowCategory).where(
                func.lower(WorkflowCategory.name) == name.lower()
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    category = WorkflowCategory(name=name, description="")
    session.add(category)
    await session.flush()
    return category


async def seed_default_skills(skills_dir: str | Path | None = None) -> None:
    """Import skills from disk into the database (idempotent).

    Reads every skill directory under *skills_dir* (default: ``.agents/skills/``
    relative to the project root), parses the YAML frontmatter from each
    ``SKILL.md``, and upserts the skill + all its files (``SKILL.md``,
    ``references/*``, ``templates/*``, ``examples/*``).

    Directories named ``.local``, ``_tooling``, or starting with ``_SHARED``
    are skipped, as are regular files (``README.md``, etc.).
    """

    import yaml

    from bheembhai.models.skill import Skill, SkillFile

    if skills_dir is None:
        # Resolve relative to project root: database.py is at shared/bheembhai/
        project_root = Path(__file__).resolve().parent.parent.parent
        skills_dir = project_root / ".agents" / "skills"

    skills_path = Path(skills_dir)
    if not skills_path.is_dir():
        _logger.warning("Skills directory not found: %s — skipping seed", skills_path)
        return

    _logger.info("Seeding skills from %s …", skills_path)
    count = 0

    for entry in sorted(skills_path.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if name in (".local", "_tooling") or name.startswith("_SHARED"):
            continue

        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            _logger.debug("Skipping %s — no SKILL.md", name)
            continue

        # Parse YAML frontmatter from SKILL.md
        content = skill_md.read_text()
        frontmatter: dict[str, str] = {}
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    fm = yaml.safe_load(parts[1])
                    if isinstance(fm, dict):
                        frontmatter = fm
                except yaml.YAMLError:
                    _logger.debug("Bad frontmatter in %s/SKILL.md — skipping", name)
                    continue

        skill_name = frontmatter.get("name", name)
        description = frontmatter.get("description", "")
        model = frontmatter.get("model", "medium")
        if model not in ("high", "medium", "low"):
            model = "medium"
        compatibility = frontmatter.get("compatibility")

        async with _sessionmaker() as session:
            from sqlalchemy import select

            # Upsert skill — platform scope only: project skills with the same
            # name must never be touched (or the lookup would be ambiguous).
            result = await session.execute(
                select(Skill).where(
                    Skill.name == skill_name, Skill.project_id.is_(None)
                )
            )
            skill = result.scalar_one_or_none()
            if skill is None:
                skill = Skill(
                    name=skill_name,
                    description=description,
                    model=model,
                    compatibility=compatibility,
                )
                session.add(skill)
                await session.flush()
                _logger.info("  Created skill: %s", skill_name)
            else:
                skill.description = description
                skill.model = model
                skill.compatibility = compatibility
                _logger.info("  Updated skill: %s", skill_name)

            # Collect all files
            file_entries: list[tuple[str, str]] = []
            # SKILL.md always included
            file_entries.append(("SKILL.md", content))
            for subdir_name in ("references", "templates", "examples"):
                subdir = entry / subdir_name
                if subdir.is_dir():
                    for sf in sorted(subdir.iterdir()):
                        if sf.is_file():
                            rel = f"{subdir_name}/{sf.name}"
                            file_entries.append((rel, sf.read_text()))

            # Upsert each file
            existing_files_result = await session.execute(
                select(SkillFile).where(SkillFile.skill_id == skill.id)
            )
            existing_by_path: dict[str, SkillFile] = {
                f.path: f for f in existing_files_result.scalars().all()
            }

            for fpath, fcontent in file_entries:
                ef = existing_by_path.pop(fpath, None)
                if ef is None:
                    session.add(SkillFile(
                        skill_id=skill.id,
                        path=fpath,
                        content=fcontent,
                    ))
                else:
                    ef.content = fcontent

            # Remove files that no longer exist on disk
            for stale in existing_by_path.values():
                await session.delete(stale)

            await session.commit()
            count += 1

    _logger.info("Skills seed complete — %d skills processed", count)


async def seed_default_workflows() -> None:
    """Import workflows and policies from disk config YAMLs into the database (idempotent).

    Reads every ``.yaml`` file under the project-root ``config/`` directory,
    detects whether each is a workflow (contains a ``workflow:`` key) or a
    policy (contains a ``policy:`` key), and upserts by (project_id, name,
    version).

    If no projects exist yet, a default "BheemBhai Platform" project is
    created first so the FK constraints are satisfied.
    """
    import yaml as _yaml
    from sqlalchemy import select

    from bheembhai.models.workflow import Policy, Workflow

    # Resolve relative to project root
    project_root = Path(__file__).resolve().parent.parent.parent
    config_dir = project_root / "config"
    if not config_dir.is_dir():
        _logger.warning("Config directory not found: %s — skipping workflow seed", config_dir)
        return

    _logger.info("Seeding workflows & policies from %s …", config_dir)

    async with _sessionmaker() as session:
        # Workflows are now project-independent templates, so no project is needed.

        # Collect YAML files and classify them
        workflows_seen: dict[str, Workflow] = {}  # name → Workflow for policy linking
        wf_count = 0
        pol_count = 0

        for yaml_file in sorted(config_dir.glob("*.yaml")):
            try:
                raw = _yaml.safe_load(yaml_file.read_text())
            except _yaml.YAMLError:
                _logger.debug("Skipping unparseable YAML: %s", yaml_file.name)
                continue
            if not isinstance(raw, dict):
                continue

            if "workflow" in raw:
                # ── Workflow ──
                name = str(raw.get("workflow", yaml_file.stem))
                version = int(raw.get("version", 1))
                yaml_content = yaml_file.read_text()

                # Top-level `category:` is required for workflows — resolved
                # get-or-create so unknown YAML categories auto-create instead
                # of failing. A workflow without a category is rejected loudly.
                category_name = str(raw.get("category") or "").strip()
                wf_category = (
                    await _get_or_create_category(session, category_name)
                    if category_name
                    else None
                )
                description = str(raw.get("description") or "").strip()

                # Upsert — look up the *platform* template (project_id IS NULL) only.
                # Project clones share the same (name, version) via partial unique indexes,
                # so we must filter to the platform row to avoid MultipleResultsFound.
                existing_result = await session.execute(
                    select(Workflow).where(
                        Workflow.project_id.is_(None),
                        Workflow.name == name,
                        Workflow.version == version,
                    )
                )
                wf = existing_result.scalar_one_or_none()
                if wf is None:
                    if wf_category is None:
                        raise ValueError(
                            f"Workflow '{name}' in {yaml_file.name} has no "
                            "category — every workflow must belong to a category"
                        )
                    wf = Workflow(
                        name=name,
                        description=description,
                        version=version,
                        yaml_content=yaml_content,
                        is_active=True,
                        workflow_category_id=wf_category.id,
                    )
                    session.add(wf)
                    await session.flush()
                    _logger.info("  Created workflow: %s v%d", name, version)
                else:
                    wf.yaml_content = yaml_content
                    if category_name:
                        # Re-assign when the YAML gains a category; leave the
                        # row untouched when the key is absent (admin edits win).
                        wf.workflow_category_id = wf_category.id if wf_category else None
                    if "description" in raw:
                        # Same rule: an absent key never clobbers admin edits.
                        wf.description = description
                    _logger.info("  Updated workflow: %s v%d", name, version)

                workflows_seen[name] = wf
                wf_count += 1

            elif "policy" in raw:
                # ── Policy ──
                name = str(raw.get("policy", yaml_file.stem))
                version = int(raw.get("version", 1))
                applies_to = str(raw.get("applies_to", ""))
                yaml_content = yaml_file.read_text()

                # We can only link to a policy's target workflow if it was already seeded
                target_wf = workflows_seen.get(applies_to)
                if target_wf is None and applies_to:
                    # Look up the platform template by name (project_id IS NULL) only,
                    # to avoid matching a project-specific clone.
                    wf_result = await session.execute(
                        select(Workflow).where(
                            Workflow.project_id.is_(None),
                            Workflow.name == applies_to,
                        )
                    )
                    target_wf = wf_result.scalar_one_or_none()

                if target_wf is None:
                    _logger.warning(
                        "  Skipping policy '%s' — workflow '%s' not found",
                        name, applies_to,
                    )
                    continue

                # Policies are also project-independent templates now.
                # Project-policy linking will be handled in project management later.

                existing_result = await session.execute(
                    select(Policy).where(
                        Policy.workflow_id == target_wf.id,
                        Policy.name == name,
                        Policy.version == version,
                    )
                )
                pol = existing_result.scalar_one_or_none()
                if pol is None:
                    pol = Policy(
                        project_id=None,
                        workflow_id=target_wf.id,
                        name=name,
                        version=version,
                        yaml_content=yaml_content,
                        is_active=True,
                    )
                    session.add(pol)
                    _logger.info("  Created policy: %s v%d → %s", name, version, applies_to)
                else:
                    pol.yaml_content = yaml_content
                    _logger.info("  Updated policy: %s v%d → %s", name, version, applies_to)

                pol_count += 1

        await session.commit()

    _logger.info(
        "Workflow seed complete — %d workflows, %d policies processed",
        wf_count, pol_count,
    )


async def get_session() -> AsyncSession:  # type: ignore[empty-body]
    """Yield an async session. Used as a FastAPI dependency.

    Commits on success, rolls back on exception — so callers only need to
    ``db.add()`` + ``await db.flush()`` and let the dependency handle the rest.
    """
    if _sessionmaker is None:
        raise RuntimeError("Database not initialised — call init_database first")
    async with _sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def close_database() -> None:
    """Dispose the engine. Call at shutdown."""
    global _engine, _sessionmaker
    if _engine:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
