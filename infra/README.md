# BheemBhai — Terraform (Pattern A: EC2 + Docker Compose)

Provisions everything the app needs in AWS. The app itself runs via
`docker compose` on a single EC2 instance (added in Phase 2).

## Two-phase deployment

### Phase 1 (now) — foundational AWS resources

`enable_ec2 = false` (the default). Creates Cognito, S3, and SSM only —
no EC2, no IAM roles, no security groups.

```bash
cd infra/
cp terraform.tfvars.example terraform.tfvars
# Edit: set aws_region, github_token, jira_api_token
# Leave enable_ec2 = false

terraform init
terraform plan    # should show 9 resources: Cognito ×2, S3 ×4, SSM ×2 + caller-identity
terraform apply

# Copy the env_snippet output into your local .env / docker-compose env
terraform output env_snippet
```

### Phase 2 (later) — add the EC2 box

When features are ready to deploy:

```bash
# 1. Set enable_ec2 = true in terraform.tfvars
# 2. Fill in ssh_key_name, git_remote_url, ssh_allowed_cidr

terraform plan    # should show 11 new resources (EC2 + IAM + SG)
terraform apply

# 3. Wait ~3 min, then open http://<ec2_public_ip>:9000
```

## Resources (full set after Phase 2)

| Resource | Phase | File | Notes |
|----------|-------|------|-------|
| Cognito User Pool | 1 | `cognito.tf` | Email alias, `USER_PASSWORD_AUTH` enabled |
| Cognito App Client | 1 | `cognito.tf` | Public (no secret), 1h access / 30d refresh |
| S3 bucket | 1 | `s3.tf` | Versioned, encrypted, public access blocked |
| SSM parameters | 1 | `ssm.tf` | GitHub token + Jira token (SecureString) |
| EC2 instance | 2 | `ec2.tf` | Amazon Linux 2023, t3.micro default |
| Security group | 2 | `ec2.tf` | Ports 8000, 8001, 22 (CIDR-restricted) |
| IAM role + policies | 2 | `ec2.tf` | SSM read, S3 read/write, Cognito admin |

## How the app lands on the box

The EC2 `user_data` script (rendered from `user_data.sh.tftpl`) runs on first boot:

1. Installs Docker + docker compose plugin + git
2. Clones the repo at the specified branch
3. Writes `.env` from Terraform variables (Cognito IDs, S3 bucket, region)
4. Runs `docker compose up -d --build`

Secrets (GitHub token, Jira token) live in SSM Parameter Store and are
fetched by the app at runtime via the IAM instance profile — no tokens
in `.env` or user-data.

## Changing a secret

SSM parameters have `ignore_changes` on `value` so Terraform won't
clobber manual updates. Change a secret directly:

```bash
aws ssm put-parameter \
  --name "/bheembhai/dev/github-token" \
  --value "new-token" \
  --type SecureString \
  --overwrite \
  --region us-east-1
```

No redeploy needed — the app reads from SSM on each request.

## SSH access

```bash
ssh -i ~/.ssh/your-key.pem ec2-user@<public_ip>
```

## Production hardening (before going live)

- [ ] Pin `ssh_allowed_cidr` to your VPN/office IP
- [ ] Switch `instance_type` to `t3.small` or larger
- [ ] Set `environment = "prod"` (enables S3 lifecycle, switches to Secrets Manager)
- [ ] Add an Elastic IP (uncomment in `ec2.tf`) or put an ALB in front
- [ ] Move state to an S3 backend with DynamoDB lock (uncomment in `main.tf`)
- [ ] Add a TLS certificate + nginx reverse proxy for HTTPS
- [ ] Enable CloudWatch agent for structured logging
- [ ] Set up RDS instead of Docker Postgres for persistence (swap `DATABASE_URL`)
- [ ] Add automated AMI backups or EBS snapshot lifecycle

## Switching to ECS Fargate later

If single-box Docker Compose outgrows itself, the migration is:

1. Push images to ECR instead of building on-box
2. Add `ecs.tf` with the ECS service, task definition, ALB, and target groups
3. Point CloudFront/Route53 at the ALB instead of the EC2 IP
4. Remove `ec2.tf`

The app code doesn't change — same containers, different runtime.
