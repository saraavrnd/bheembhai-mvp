#!/bin/sh
# Provision AWS resources in LocalStack so the dev environment mirrors production.
# Run once at container start (localstack-setup service in docker-compose).
# All resources use dummy credentials — LocalStack accepts anything.

set -eu

ENDPOINT="http://localstack:4566"
REGION="us-east-1"

echo "=== Provisioning LocalStack resources ==="

# ── S3: artifact bucket ───────────────────────────────────────
echo "→ Creating S3 bucket: bheembhai-artifacts"
aws s3 mb "s3://bheembhai-artifacts" \
  --endpoint-url "$ENDPOINT" \
  --region "$REGION" \
  2>/dev/null || echo "   (bucket already exists)"

# ── Secrets Manager: placeholder secrets ──────────────────────
# In real prod these are created by the Platform API at integration setup time.
# Here we pre-create them so the Engine can fetch them at step-launch time.
for secret_name in dev-github-token dev-jira-token; do
  echo "→ Creating secret: $secret_name"
  aws secretsmanager create-secret \
    --name "$secret_name" \
    --secret-string '{"token":"dev-placeholder"}' \
    --endpoint-url "$ENDPOINT" \
    --region "$REGION" \
    2>/dev/null || echo "   (secret already exists)"
done

# ── Cognito: user pool + client + test user ───────────────────
# NOTE: Cognito is a LocalStack Pro feature. In the free tier it is unavailable.
# The app uses DEV_AUTH_BYPASS=true locally, so Cognito is optional for dev.
echo "→ Creating Cognito user pool (Pro-only — will fail gracefully on free tier)"
POOL_ID=""
CLIENT_ID=""

if POOL_ID=$(aws cognito-idp create-user-pool \
  --pool-name bheembhai-dev \
  --alias-attributes email \
  --auto-verified-attributes email \
  --policies '{"PasswordPolicy":{"MinimumLength":8,"RequireUppercase":true,"RequireLowercase":true,"RequireNumbers":true,"RequireSymbols":false}}' \
  --schema '[
    {"Name":"email","AttributeDataType":"String","Required":true,"Mutable":true},
    {"Name":"name","AttributeDataType":"String","Required":false,"Mutable":true}
  ]' \
  --endpoint-url "$ENDPOINT" \
  --region "$REGION" \
  --query 'UserPool.Id' \
  --output text 2>/dev/null); then
  echo "   Pool ID: $POOL_ID"

  echo "→ Creating Cognito user pool client"
  CLIENT_ID=$(aws cognito-idp create-user-pool-client \
    --user-pool-id "$POOL_ID" \
    --client-name bheembhai-dev-client \
    --no-generate-secret \
    --explicit-auth-flows "ALLOW_USER_PASSWORD_AUTH,ALLOW_REFRESH_TOKEN_AUTH,ALLOW_USER_SRP_AUTH" \
    --endpoint-url "$ENDPOINT" \
    --region "$REGION" \
    --query 'UserPoolClient.ClientId' \
    --output text)
  echo "   Client ID: $CLIENT_ID"

  echo "→ Creating test user (dev@bheembhai.local / DevPass123!)"
  aws cognito-idp admin-create-user \
    --user-pool-id "$POOL_ID" \
    --username "dev@bheembhai.local" \
    --user-attributes \
      "Name=email,Value=dev@bheembhai.local" \
      "Name=name,Value=Dev User" \
      "Name=email_verified,Value=true" \
    --temporary-password "DevPass123!" \
    --message-action SUPPRESS \
    --endpoint-url "$ENDPOINT" \
    --region "$REGION" \
    2>/dev/null || echo "   (test user already exists)"

  # Set a permanent password (skip the NEW_PASSWORD_REQUIRED challenge)
  aws cognito-idp admin-set-user-password \
    --user-pool-id "$POOL_ID" \
    --username "dev@bheembhai.local" \
    --password "DevPass123!" \
    --permanent \
    --endpoint-url "$ENDPOINT" \
    --region "$REGION" \
    2>/dev/null || echo "   (password already set)"
else
  echo "   Cognito not available (LocalStack Pro feature) — skipping"
  echo "   Dev auth bypass is enabled (DEV_AUTH_BYPASS=true)"
fi

echo ""
echo "=== LocalStack provisioned ==="
echo "   S3:          s3://bheembhai-artifacts"
if [ -n "$POOL_ID" ]; then
  echo "   Cognito:     pool=$POOL_ID  client=$CLIENT_ID"
  echo "   Test user:   dev@bheembhai.local / DevPass123!"
else
  echo "   Cognito:     skipped (Pro-only, not needed with DEV_AUTH_BYPASS)"
fi
echo "   Endpoint:    $ENDPOINT"
