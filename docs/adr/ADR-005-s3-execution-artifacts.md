# ADR-005: S3 for execution artifacts

**Status:** Accepted · **Date:** 2026-08-10 · **Deciders:** Saraav

## Context

Today, each step's output lives on the host filesystem: `$BB_WORKDIR/results/<run_id>/<step_id>/
<attempt_no>/`. The files include `bb_step_result.json`, `agent.log`, `diagnostics.txt`, and
`summary.txt`. The Platform API serves these via the `/api/runs/{id}/file?path=` endpoint with
a 2 MB cap and path-traversal guards.

With Fargate-based execution, the agent container runs on an ephemeral Fargate task with no
persistent filesystem. The host EC2 instance is no longer the container runtime — artifacts
must survive task teardown and be accessible to both the Engine Service (for reconciliation)
and the browser (for the file viewer at approval gates).

## Decision

**S3 for execution artifacts.** The agent container writes results directly to S3 (via the AWS
SDK or boto3 available in the container). The Engine Service reads results from S3 during
reconciliation. The Platform API generates pre-signed URLs for the browser file viewer.

S3 key structure: `s3://<bucket>/runs/<run_id>/<step_id>/<attempt_no>/bb_step_result.json`
(and `agent.log`, `diagnostics.txt`, `summary.txt`).

The local filesystem path (`$BB_WORKDIR/results/...`) is replaced entirely by S3. The
Fargate task's IAM role is scoped to the specific run prefix so agents can only write to
their own run's path.

## Alternatives considered

- **EFS mounted to Fargate tasks (rejected):** Gives a shared filesystem that survives task
  teardown. But EFS has throughput scaling concerns (burst credits), requires VPC configuration,
  and couples the Fargate tasks to a specific AZ. S3 is simpler, cheaper at this scale, and
  already designed in the README.
- **Agent writing to a webhook/API (rejected):** The agent would POST its result to the Engine
  Service. But this makes the Engine a runtime dependency for the agent (if the Engine is
  down, the result is lost). S3 decouples them: the agent writes, the Engine reads when ready.
- **Keep local disk + EFS (rejected):** The current pattern of host-mounted volumes doesn't
  translate to Fargate (no persistent host).

## Consequences

- **Easier:** Artifacts survive any failure — Fargate task crash, Engine restart, EC2
  replacement. S3 durability is 99.999999999% (11 9s).
- **Easier:** Signed URLs for the browser file viewer — no need for the Platform API to proxy
  large files through EC2. The 2 MB cap and path-traversal guard are replaced by S3's own
  access control.
- **Easier:** Lifecycle policies for cost management — auto-delete artifacts older than 90
  days. Per-project prefixes enable cost attribution.
- **Harder:** Agent container needs AWS credentials (task IAM role) and the boto3/SDK to
  write to S3. The current container image (`node:20-slim`) doesn't include the AWS SDK —
  needs either `awscli` or a Python `boto3` call added to `run_skill.sh`. (The container
  already has Python 3 per the Dockerfile.)
- **Harder:** Network dependency — the agent must reach S3 from the Fargate task. This
  requires a VPC endpoint for S3 or a NAT gateway in the Fargate subnet. Standard Fargate
  networking, but worth calling out.
