# Shared Run Ledger Hook

The shared run ledger is the local audit trail for skill execution and revert planning.

## Location
- Default: `.local/beembhai/run-ledger.jsonl`
- Override: `BEEMBHAI_RUN_LEDGER_PATH` or `BEEBHAI_RUN_LEDGER_PATH`

## Writer
- `.agents/skills/_tooling/run_ledger.py`

## Record shape
Each JSONL line is one append-only event with:
- `run_id`
- `parent_run_id` / `supersedes_run_id` when applicable
- `story_key`
- `skill_name`
- `branch`
- `step`
- `event`
- `status`
- `timestamp`
- `changed_files`
- `commits`
- `external_actions`
- `reversible`
- `notes`
- `metadata`

## Typical lifecycle
1. Append `started`.
2. Append step outcomes as the skill progresses.
3. Append `completed`, `failed`, or `reverted`.
4. Never delete prior entries.

## CLI usage
```bash
python .agents/skills/_tooling/run_ledger.py record \
  --event started \
  --story-key BEEM-16 \
  --skill-name story-implement \
  --branch feat/BEEM-16-bootstrap

python .agents/skills/_tooling/run_ledger.py preview --story-key BEEM-16
python .agents/skills/_tooling/run_ledger.py latest --story-key BEEM-16
```

## Notes
- The ledger is local-only and gitignored.
- The hook is append-only.
- Revert workflows should preview from the ledger before touching files or commits.
