# ── Cognito User Pool ──────────────────────────────────────────────
resource "aws_cognito_user_pool" "bheembhai" {
  name = "${local.name_prefix}-users"

  alias_attributes         = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length    = 8
    require_uppercase = true
    require_lowercase = true
    require_numbers   = true
    require_symbols   = false
  }

  # Keep the dev pool simple — no MFA, no advanced security
  mfa_configuration = "OFF"

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  tags = { Name = "${local.name_prefix}-users" }
}

# ── Cognito App Client ─────────────────────────────────────────────
resource "aws_cognito_user_pool_client" "bheembhai" {
  name         = "${local.name_prefix}-client"
  user_pool_id = aws_cognito_user_pool.bheembhai.id

  generate_secret = false # public client (SPA / API auth)

  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  callback_urls = var.cognito_callback_urls

  # Token validity: 1-hour access, 30-day refresh
  access_token_validity  = 1
  refresh_token_validity = 30
  token_validity_units {
    access_token  = "hours"
    refresh_token = "days"
  }
}
