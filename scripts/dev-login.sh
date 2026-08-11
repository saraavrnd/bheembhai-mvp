#!/bin/bash
# Sign in as the test user against LocalStack Cognito and print the JWT.
# The printed token can be used as a Bearer token for API calls:
#   curl -H "Authorization: Bearer $(./scripts/dev-login.sh)" http://localhost:8000/api/projects

set -eu

ENDPOINT="${LOCALSTACK_ENDPOINT:-http://localhost:4566}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
CLIENT_ID="${COGNITO_CLIENT_ID:-dev-client-id}"
USERNAME="${1:-dev@bheembhai.local}"
PASSWORD="${2:-DevPass123!}"

# First try to find the pool ID
POOL_ID=$(aws cognito-idp list-user-pools \
  --max-results 10 \
  --endpoint-url "$ENDPOINT" \
  --region "$REGION" \
  --query "UserPools[?Name=='bheembhai-dev'].Id | [0]" \
  --output text 2>/dev/null)

if [ -z "$POOL_ID" ] || [ "$POOL_ID" = "None" ]; then
  echo "ERROR: Cognito pool not found. Run localstack-setup first." >&2
  exit 1
fi

# Find the actual client ID
ACTUAL_CLIENT_ID=$(aws cognito-idp list-user-pool-clients \
  --user-pool-id "$POOL_ID" \
  --max-results 10 \
  --endpoint-url "$ENDPOINT" \
  --region "$REGION" \
  --query "UserPoolClients[?ClientName=='bheembhai-dev-client'].ClientId | [0]" \
  --output text 2>/dev/null)

if [ -z "$ACTUAL_CLIENT_ID" ] || [ "$ACTUAL_CLIENT_ID" = "None" ]; then
  echo "ERROR: Client not found. Run localstack-setup first." >&2
  exit 1
fi

# Initiate auth
AUTH_RESULT=$(aws cognito-idp initiate-auth \
  --client-id "$ACTUAL_CLIENT_ID" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters "USERNAME=$USERNAME,PASSWORD=$PASSWORD" \
  --endpoint-url "$ENDPOINT" \
  --region "$REGION" \
  --query 'AuthenticationResult.IdToken' \
  --output text 2>/dev/null)

if [ -z "$AUTH_RESULT" ] || [ "$AUTH_RESULT" = "None" ]; then
  echo "ERROR: Auth failed for $USERNAME. Check username/password." >&2
  exit 1
fi

echo "$AUTH_RESULT"
