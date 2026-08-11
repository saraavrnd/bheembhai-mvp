# ── Outputs ─────────────────────────────────────────────────────────

output "ec2_public_ip" {
  description = "Public IP — open http://<this>:8000 in a browser"
  value       = aws_instance.app.public_ip
}

output "ec2_public_dns" {
  description = "Public DNS name"
  value       = aws_instance.app.public_dns
}

output "ssh_command" {
  description = "SSH one-liner"
  value       = "ssh -i ~/.ssh/${var.ssh_key_name}.pem ec2-user@${aws_instance.app.public_ip}"
}

output "cognito_user_pool_id" {
  description = "Cognito User Pool ID — set as COGNITO_USER_POOL_ID in .env"
  value       = aws_cognito_user_pool.bheembhai.id
}

output "cognito_client_id" {
  description = "Cognito App Client ID — set as COGNITO_CLIENT_ID in .env"
  value       = aws_cognito_user_pool_client.bheembhai.id
}

output "s3_bucket_name" {
  description = "S3 bucket for step artifacts"
  value       = aws_s3_bucket.artifacts.bucket
}

output "ssm_github_token_param" {
  description = "SSM parameter name for the GitHub token"
  value       = aws_ssm_parameter.github_token.name
}

output "ssm_jira_token_param" {
  description = "SSM parameter name for the Jira API token"
  value       = aws_ssm_parameter.jira_token.name
}
