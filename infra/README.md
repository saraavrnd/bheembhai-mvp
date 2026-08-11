# BheemBhai — Terraform (Pattern A: EC2 + Docker Compose)

Provisions everything the app needs in AWS. The app itself runs via
`docker compose` on a single EC2 instance.

## Resources created

| Resource | File | Notes |
|----------|------|-------|
| EC2 instance | `ec2.tf` | Amazon Linux 2023, t3.micro default |
| Security group | `ec2.tf` | Ports 8000, 8001, 22 (CIDR-restricted) |
| IAM role + policies | `ec2.tf` | SSM read, S3 read/write, Cognito admin |
| Cognito User Pool | `cognito.tf` | Email alias, `USER_PASSWORD_AUTH` enabled |
| Cognito App Client | `cognito.tf` | Public (no secret), 1h access / 30d refresh |
| S3 bucket | `s3.tf` | Versioned, encrypted, public access blocked |
| SSM parameters | `ssm.tf` | GitHub token + Jira token (SecureString) |

## Quick start

```bash
# 1. One-time: initialise
cd infra/
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your values

# 2. See what will be created
terraform plan

# 3. Apply
terraform apply

# 4. Wait ~3 min for user-data to finish, then open:
#    http://<ec2_public_ip>:8000
```

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
