# Local Development Guide

## Architecture of the dev environment

```
┌──────────────────────────────────────────────────────────┐
│                    docker-compose                        │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐                     │
│  │ platform-api │  │engine-service│                     │
│  │   :9000      │  │   :9001      │                     │
│  │  (--reload)  │  │  (--reload)  │                     │
│  └──────┬───────┘  └──────┬───────┘                     │
│         │                 │                              │
│         └────────┬────────┘                              │
│                  │                                       │
│           ┌──────┴──────┐                               │
│           │  postgres:16 │                               │
│           │    :5555     │                               │
│           └─────────────┘                               │
└──────────────────────────────────────────────────────────┘

                          │                              │
                          │  AWS SDK (boto3)              │
                          │  resolves credentials from:   │
                          │  ~/.aws/config, env vars,     │
                          │  or instance profile          │
                          ▼                              ▼
               ┌──────────────────────────────────────────┐
               │           AWS (DEV-tagged)               │
               │                                          │
               │  Cognito  │   S3     │  Secrets Mgr     │
               │  (users)  │ (artifacts)│ (tokens)        │
               └──────────────────────────────────────────┘
```

AWS resources (Cognito User Pool, S3 bucket, Secrets Manager secrets) are provisioned in a real AWS account and **tagged `Environment: DEV`** for cost tracking and lifecycle management. The AWS SDK resolves credentials from your environment — no `AWS_ENDPOINT_URL` or dummy credentials needed.

## Quick start (first time)

```bash
# 1. Clone and set up env
cp .env.example .env
# Set ANTHROPIC_API_KEY if you'll run skills

# 2. Ensure AWS credentials are available (any one works):
#    - ~/.aws/config + ~/.aws/credentials
#    - AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY env vars
#    - AWS_PROFILE pointing at your dev profile
aws sts get-caller-identity  # verify

# 3. Start everything (Postgres + both services)
docker-compose up -d

# 4. Verify
curl http://localhost:9000/health           # Platform API
curl http://localhost:9001/engine/health    # Engine Service

# 5. Login (dev mode — any credentials work)
open http://localhost:9000/login
```

## Auth: two modes

### Dev mode (default — no Cognito needed)

`DEV_AUTH_BYPASS=true` returns a hardcoded dev identity for every request.
All API calls work without tokens. This is what docker-compose uses by default.

```bash
curl http://localhost:9000/api/projects  # works — dev identity
```

### Real auth mode (AWS Cognito)

1. Provision a Cognito User Pool + App Client in your AWS account (tag `Environment: DEV`).
   The App Client must have `USER_PASSWORD_AUTH` enabled.
2. Set the pool/client IDs in `.env` or docker-compose env:
   ```yaml
   DEV_AUTH_BYPASS: "false"
   COGNITO_USER_POOL_ID: us-east-1_xxxxxxxxx
   COGNITO_CLIENT_ID: xxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
3. Restart: `docker-compose restart platform-api`
4. Login at `http://localhost:9000/login` with a real Cognito user

## AWS resources (DEV-tagged)

Provision these once in your AWS dev account. All resources should carry
`Environment: DEV` so they're easy to find, track, and clean up.

| Resource | Name / pattern | Tag | Purpose |
|----------|---------------|-----|---------|
| Cognito User Pool | `bheembhai-dev` | `Environment=DEV` | Dev users + JWT issuer |
| Cognito App Client | `bheembhai-dev-client` | — | `USER_PASSWORD_AUTH` flow |
| S3 bucket | `bheembhai-artifacts-dev` | `Environment=DEV` | Step artifacts, logs |
| SSM Parameter | `/bheembhai/dev/github-token` | `Environment=DEV` | GitHub token for integration |
| SSM Parameter | `/bheembhai/dev/jira-token` | `Environment=DEV` | Jira token for integration |

### Example: creating Cognito via AWS CLI

```bash
# Create the user pool
aws cognito-idp create-user-pool \
  --pool-name bheembhai-dev \
  --alias-attributes email \
  --auto-verified-attributes email \
  --policies '{"PasswordPolicy":{"MinimumLength":8,"RequireUppercase":true,"RequireLowercase":true,"RequireNumbers":true,"RequireSymbols":false}}' \
  --tags "Environment=DEV" \
  --region us-east-1

# Create the app client
POOL_ID="us-east-1_xxxxxxxxx"
aws cognito-idp create-user-pool-client \
  --user-pool-id "$POOL_ID" \
  --client-name bheembhai-dev-client \
  --no-generate-secret \
  --explicit-auth-flows "ALLOW_USER_PASSWORD_AUTH,ALLOW_REFRESH_TOKEN_AUTH" \
  --region us-east-1

# Create a test user
aws cognito-idp sign-up \
  --client-id "xxxxxxxxxxxxxxxxxxxxxxxxxx" \
  --username "dev@bheembhai.local" \
  --password "DevPass123!" \
  --user-attributes "Name=email,Value=dev@bheembhai.local" "Name=name,Value=Dev User" \
  --region us-east-1

# Confirm the user (skip email verification for dev)
aws cognito-idp admin-confirm-sign-up \
  --user-pool-id "$POOL_ID" \
  --username "dev@bheembhai.local" \
  --region us-east-1
```

### Example: storing secrets in SSM Parameter Store

```bash
# Store GitHub token as a SecureString parameter
aws ssm put-parameter \
  --name "/bheembhai/dev/github-token" \
  --value "ghp_xxxxxxxxxxxxxxxxxxxx" \
  --type SecureString \
  --tags "Key=Environment,Value=DEV" \
  --region us-east-1

# Store Jira API token
aws ssm put-parameter \
  --name "/bheembhai/dev/jira-token" \
  --value "your-jira-api-token" \
  --type SecureString \
  --tags "Key=Environment,Value=DEV" \
  --region us-east-1

# Verify (shows decrypted value)
aws ssm get-parameter \
  --name "/bheembhai/dev/github-token" \
  --with-decryption \
  --region us-east-1
```

### Switching to Secrets Manager later (production)

When you need rotation, cross-account sharing, or password generation:

```bash
# 1. Change one env var
SECURE_STORAGE_BACKEND=aws_secrets_manager

# 2. Migrate existing secrets
aws secretsmanager create-secret \
  --name "dev/github-token" \
  --secret-string "$(aws ssm get-parameter --name /bheembhai/dev/github-token --with-decryption --query 'Parameter.Value' --output text)" \
  --tags "Key=Environment,Value=DEV"

# 3. Update code references from SSM paths to Secrets Manager names
#    (or keep them the same — both accept arbitrary string refs)
```

No code changes needed beyond the env var — the `SecureStorage` protocol absorbs the rest.

## Running without Docker (fast iteration)

When iterating on code, run services directly on the host while
keeping Postgres in Docker:

```bash
# Terminal 1: Postgres only
docker-compose up -d postgres

# Terminal 2: Platform API
cd platform_api
DATABASE_URL=postgresql+asyncpg://bheembhai:bheembhai@localhost:5555/bheembhai \
DEV_AUTH_BYPASS=true \
uvicorn main:app --port 9000 --reload

# Terminal 3: Engine Service
cd engine_service
DATABASE_URL=postgresql+asyncpg://bheembhai:bheembhai@localhost:5555/bheembhai \
ENGINE_ID=dev-1 \
uvicorn main:app --port 9001 --reload
```

## Testing

```bash
# Unit + integration (no Docker needed)
pytest tests/unit/ tests/integration/ -v

# E2E (needs the stack running)
docker-compose up -d
pytest tests/e2e/ -v

# Existing engine tests (FakeRuntime, no Docker)
python3 test_engine.py
```

## Common workflows

### Reset everything
```bash
docker-compose down -v   # destroys volumes (DB data)
docker-compose up -d     # fresh start
```

### Run database migrations
```bash
cd shared
DATABASE_URL=postgresql+asyncpg://bheembhai:bheembhai@localhost:5555/bheembhai \
  alembic upgrade head
```

### Add a Cognito test user
```bash
aws cognito-idp sign-up \
  --client-id "$COGNITO_CLIENT_ID" \
  --username "reviewer@bheembhai.local" \
  --password "ReviewerPass123!" \
  --user-attributes "Name=email,Value=reviewer@bheembhai.local" \
  --region us-east-1

aws cognito-idp admin-confirm-sign-up \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --username "reviewer@bheembhai.local" \
  --region us-east-1
```

## What stays local vs what hits AWS

| Thing | Local | AWS | Why |
|-------|-------|-----|-----|
| Postgres (data) | Docker | — | Fast dev cycle, no RDS cost |
| Platform API + Engine | Docker | — | Code you're changing |
| Cognito (auth) | — | AWS | Real JWT issuance + validation |
| S3 (artifacts) | — | AWS | Persistent, same API as prod |
| SSM Parameter Store | — | AWS | Free, same SDK; swap to Secrets Manager for rotation |
| ECS Fargate | — | AWS | Only when testing Fargate launches |
