# ── Provider + Backend ─────────────────────────────────────────────
# State lives locally by default. For team use, uncomment the S3 backend:
# terraform {
#   backend "s3" {
#     bucket  = "bheembhai-tfstate-dev"
#     key     = "terraform.tfstate"
#     region  = "us-east-1"
#     encrypt = true
#   }
# }

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Environment = var.environment
      Project     = "bheembhai"
      ManagedBy   = "terraform"
    }
  }
}

# ── Locals ─────────────────────────────────────────────────────────
locals {
  name_prefix = "bheembhai-${var.environment}"

  # When to swap to Secrets Manager (prod): change this + flip docker-compose env
  secure_storage_backend = var.environment == "prod" ? "aws_secrets_manager" : "aws_ssm"
}
