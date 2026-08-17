"""One-off data fix (BEEM-24, 2026-08-14): add the missing BLOCK route to story-design.

The seed shipped `policy-governed.yaml` gating story-design on `BLOCK` while
`workflow-story-delivery.yaml` had no `BLOCK` route from story-design. The engine's
`validate_pairing` fails init on that (a human would approve into a dead end).

The YAML sources are fixed; this script repairs rows already in the database.
It is surgical: it only rewrites the story-design ``on:`` block in workflow rows
that still carry the old seed text verbatim (rows with edits are left alone and
reported), then re-validates every workflow/policy pair in the DB.

Run:  python3 scripts/fix_story_design_block_route.py   (DATABASE_URL in env)
"""

import asyncio
import os

OLD_ON = """    "on":
      completed: test-creator
      changes_requested: story-design
      escalation_required: tech-design"""

NEW_ON = """    "on":
      completed: test-creator
      changes_requested: story-design
      escalation_required: tech-design
      BLOCK: story-design"""


async def main() -> None:
    from bheembhai.config import DatabaseConfig
    from bheembhai.database import init_database, get_sessionmaker
    from bheembhai.models.workflow import Policy, Workflow
    from engine_service.workflow import PolicySpec, WorkflowSpec, validate_pairing

    url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://bheembhai-mvp:bheembhai-mvp@localhost:5555/bheembhai-mvp",
    )
    init_database(DatabaseConfig(url=url))
    sm = get_sessionmaker()

    async with sm() as s:
        from sqlalchemy import select

        wfs = (await s.execute(select(Workflow))).scalars().all()
        updated = 0
        for w in wfs:
            content = w.yaml_content or ""
            if "BLOCK: story-design" in content:
                continue
            if OLD_ON in content:
                w.yaml_content = content.replace(OLD_ON, NEW_ON)
                updated += 1
                print(f"updated workflow id={w.id} project={w.project_id} name={w.name}")
            else:
                print(
                    f"SKIPPED workflow id={w.id} name={w.name} — story-design block "
                    "differs from the seed (has it been edited?); fix manually"
                )
        await s.commit()
        print(f"{updated} workflow row(s) updated")

    async with sm() as s:
        from sqlalchemy import select

        wfs = {w.id: w for w in (await s.execute(select(Workflow))).scalars().all()}
        pols = (await s.execute(select(Policy))).scalars().all()
        bad = 0
        for p in pols:
            wf = wfs.get(p.workflow_id)
            if wf is None:
                print(f"ORPHAN policy {p.name} (wf_id={p.workflow_id})")
                bad += 1
                continue
            try:
                validate_pairing(
                    WorkflowSpec.load_yaml(wf.yaml_content),
                    PolicySpec.load_yaml(p.yaml_content),
                )
                print(f"OK   {p.name} (project={p.project_id})")
            except Exception as e:  # noqa: BLE001 — report and continue
                bad += 1
                print(f"FAIL {p.name} (project={p.project_id}): {e}")
        if bad == 0:
            print("ALL WORKFLOW/POLICY PAIRS VALID")


if __name__ == "__main__":
    asyncio.run(main())
