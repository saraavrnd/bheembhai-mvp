# ── Input Variables ─────────────────────────────────────────────────

variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment stage (dev, staging, prod)"
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be dev, staging, or prod."
  }
}

# ── EC2 ────────────────────────────────────────────────────────────
variable "instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t3.micro" # free-tier eligible
}

variable "instance_ami" {
  description = "AMI ID (leave blank to use latest Amazon Linux 2023)"
  type        = string
  default     = ""
}

variable "ebs_volume_size_gb" {
  description = "Root EBS volume size in GB"
  type        = number
  default     = 30
}

variable "ssh_key_name" {
  description = "Existing EC2 key-pair name for SSH access"
  type        = string
}

variable "ssh_allowed_cidr" {
  description = "CIDR allowed to SSH into the instance"
  type        = string
  default     = "0.0.0.0/0" # restrict this to your IP in tfvars

  validation {
    condition     = can(cidrnetmask(var.ssh_allowed_cidr))
    error_message = "Must be a valid CIDR block."
  }
}

# ── Application ────────────────────────────────────────────────────
variable "git_remote_url" {
  description = "Git remote URL to clone the app from"
  type        = string
}

variable "git_source_branch" {
  description = "Branch to deploy"
  type        = string
  default     = "main"
}

variable "app_secret_key" {
  description = "Secret key for session signing (generate: openssl rand -hex 32)"
  type        = string
  sensitive   = true
}

# ── Auth (Cognito) ─────────────────────────────────────────────────
variable "cognito_callback_urls" {
  description = "Allowed callback URLs for the Cognito app client"
  type        = list(string)
  default     = ["http://localhost:8000"]
}

# ── Secrets (SSM — keep these out of version control) ──────────────
variable "github_token" {
  description = "GitHub personal access token for agent integrations"
  type        = string
  sensitive   = true
  default     = "" # set in tfvars
}

variable "jira_api_token" {
  description = "Jira API token for agent integrations"
  type        = string
  sensitive   = true
  default     = ""
}

variable "jira_url" {
  description = "Jira instance URL"
  type        = string
  default     = ""
}

variable "jira_email" {
  description = "Jira user email for API access"
  type        = string
  default     = ""
}

variable "anthropic_api_key" {
  description = "Anthropic API key for Claude Code agent"
  type        = string
  sensitive   = true
  default     = ""
}
