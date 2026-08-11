# ── SSM Parameter Store — App Secrets ──────────────────────────────
# These are SecureString parameters; only the EC2 IAM role can read them.

resource "aws_ssm_parameter" "github_token" {
  name        = "/bheembhai/${var.environment}/github-token"
  description = "GitHub PAT for agent integrations"
  type        = "SecureString"
  value       = var.github_token != "" ? var.github_token : "placeholder-change-me"

  tags = { Name = "${local.name_prefix}-github-token" }

  lifecycle {
    ignore_changes = [value] # don't overwrite manual updates in the console
  }
}

resource "aws_ssm_parameter" "jira_token" {
  name        = "/bheembhai/${var.environment}/jira-token"
  description = "Jira API token for agent integrations"
  type        = "SecureString"
  value       = var.jira_api_token != "" ? var.jira_api_token : "placeholder-change-me"

  tags = { Name = "${local.name_prefix}-jira-token" }

  lifecycle {
    ignore_changes = [value]
  }
}
