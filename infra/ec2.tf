# ── Amazon Linux 2023 AMI (data source — always available) ──────────
data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023*-x86_64"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# ═══════════════════════════════════════════════════════════════════════
# Everything below is gated behind enable_ec2.
# Set enable_ec2 = true in terraform.tfvars when you're ready to deploy.
# ═══════════════════════════════════════════════════════════════════════

# ── IAM Role + Instance Profile ────────────────────────────────────
resource "aws_iam_role" "app" {
  count = var.enable_ec2 ? 1 : 0
  name  = "${local.name_prefix}-ec2-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Policy: read SSM secrets the app needs at boot
resource "aws_iam_policy" "ssm_read" {
  count       = var.enable_ec2 ? 1 : 0
  name        = "${local.name_prefix}-ssm-read"
  description = "Allow EC2 to read app secrets from SSM Parameter Store"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter", "ssm:GetParametersByPath"]
      Resource = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/bheembhai/${var.environment}/*"]
    }]
  })
}

# Policy: S3 read/write for the artifacts bucket
resource "aws_iam_policy" "s3_artifacts" {
  count       = var.enable_ec2 ? 1 : 0
  name        = "${local.name_prefix}-s3-artifacts"
  description = "Allow EC2 to read/write the artifacts S3 bucket"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:GetObject", "s3:PutObject", "s3:ListBucket",
        "s3:DeleteObject", "s3:GetObjectTagging", "s3:PutObjectTagging",
      ]
      Resource = [
        aws_s3_bucket.artifacts.arn,
        "${aws_s3_bucket.artifacts.arn}/*",
      ]
    }]
  })
}

# Policy: Cognito admin actions (for user management from the app)
resource "aws_iam_policy" "cognito_admin" {
  count       = var.enable_ec2 ? 1 : 0
  name        = "${local.name_prefix}-cognito-admin"
  description = "Allow EC2 to call Cognito admin APIs"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "cognito-idp:AdminGetUser",
        "cognito-idp:AdminCreateUser",
        "cognito-idp:AdminConfirmSignUp",
        "cognito-idp:AdminDeleteUser",
        "cognito-idp:AdminUpdateUserAttributes",
        "cognito-idp:ListUsers",
      ]
      Resource = [aws_cognito_user_pool.bheembhai.arn]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  count      = var.enable_ec2 ? 1 : 0
  role       = aws_iam_role.app[0].name
  policy_arn = aws_iam_policy.ssm_read[0].arn
}

resource "aws_iam_role_policy_attachment" "s3" {
  count      = var.enable_ec2 ? 1 : 0
  role       = aws_iam_role.app[0].name
  policy_arn = aws_iam_policy.s3_artifacts[0].arn
}

resource "aws_iam_role_policy_attachment" "cognito" {
  count      = var.enable_ec2 ? 1 : 0
  role       = aws_iam_role.app[0].name
  policy_arn = aws_iam_policy.cognito_admin[0].arn
}

resource "aws_iam_instance_profile" "app" {
  count = var.enable_ec2 ? 1 : 0
  name  = "${local.name_prefix}-ec2-profile"
  role  = aws_iam_role.app[0].name
}

# ── Security Group ─────────────────────────────────────────────────
resource "aws_security_group" "app" {
  count       = var.enable_ec2 ? 1 : 0
  name        = "${local.name_prefix}-sg"
  description = "BheemBhai app — HTTP + SSH"

  ingress {
    description = "Platform API"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Engine Service"
    from_port   = 8001
    to_port     = 8001
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ssh_allowed_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-sg" }
}

# ── EC2 Instance ───────────────────────────────────────────────────
resource "aws_instance" "app" {
  count                = var.enable_ec2 ? 1 : 0
  ami                  = var.instance_ami != "" ? var.instance_ami : data.aws_ami.al2023.id
  instance_type        = var.instance_type
  key_name             = var.ssh_key_name
  iam_instance_profile = aws_iam_instance_profile.app[0].name
  security_groups      = [aws_security_group.app[0].name]

  root_block_device {
    volume_type = "gp3"
    volume_size = var.ebs_volume_size_gb
    encrypted   = true
    tags        = { Name = "${local.name_prefix}-root" }
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    git_remote_url         = var.git_remote_url
    git_source_branch      = var.git_source_branch
    environment            = var.environment
    app_secret_key         = var.app_secret_key
    aws_region             = var.aws_region
    cognito_user_pool_id   = aws_cognito_user_pool.bheembhai.id
    cognito_client_id      = aws_cognito_user_pool_client.bheembhai.id
    s3_bucket              = aws_s3_bucket.artifacts.bucket
    secure_storage_backend = local.secure_storage_backend
  })

  tags = { Name = "${local.name_prefix}-app" }
}

# ── Elastic IP (optional — uncomment for a stable IP) ──────────────
# resource "aws_eip" "app" {
#   count    = var.enable_ec2 ? 1 : 0
#   instance = aws_instance.app[0].id
#   tags     = { Name = "${local.name_prefix}-eip" }
# }
