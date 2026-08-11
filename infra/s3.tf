# ── S3 — Artifact Storage ──────────────────────────────────────────
resource "aws_s3_bucket" "artifacts" {
  bucket = "${local.name_prefix}-artifacts-${data.aws_caller_identity.current.account_id}"
  # ^ account_id suffix keeps the bucket name globally unique
}

# Block all public access (artifacts are private by default)
resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket = aws_s3_bucket.artifacts.bucket

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.bucket
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.bucket

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.bucket

  # Auto-clean old artifact versions after 90 days
  rule {
    id     = "expire-old-versions"
    status = var.environment == "prod" ? "Enabled" : "Disabled"

    filter {} # applies to all objects in the bucket

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}
