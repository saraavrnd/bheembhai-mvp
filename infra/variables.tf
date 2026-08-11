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
variable "enable_ec2" {
  description = "Provision EC2 + IAM + SG (default false — set to true when ready to deploy the box)"
  type        = bool
  default     = false
}

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
  description = "Existing EC2 key-pair name for SSH access (only needed when enable_ec2 = true)"
  type        = string
  default     = ""
}

variable "ssh_allowed_cidr" {
  description = "CIDR allowed to SSH into the instance (only needed when enable_ec2 = true)"
  type        = string
  default     = "0.0.0.0/0"

  validation {
    condition     = can(cidrnetmask(var.ssh_allowed_cidr))
    error_message = "Must be a valid CIDR block."
  }
}

# ── Application ────────────────────────────────────────────────────
variable "git_remote_url" {
  description = "Git remote URL to clone the app from (only needed when enable_ec2 = true)"
  type        = string
  default     = ""
}

variable "git_source_branch" {
  description = "Branch to deploy"
  type        = string
  default     = "main"
}

variable "app_secret_key" {
  description = "Secret key for session signing — only needed when enable_ec2 = true (generate: openssl rand -hex 32)"
  type        = string
  sensitive   = true
  default     = ""
}

# ── Auth (Cognito) ─────────────────────────────────────────────────
variable "cognito_callback_urls" {
  description = "Allowed callback URLs for the Cognito app client"
  type        = list(string)
  default     = ["http://localhost:8000"]
}
