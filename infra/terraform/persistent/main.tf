variable "aws_profile" {
  description = "AWS shared configuration profile."
  type        = string
  default     = "mlops-dev"
}

variable "aws_region" {
  description = "AWS region for persistent resources."
  type        = string
  default     = "ap-southeast-1"
}

variable "environment" {
  description = "Deployment environment."
  type        = string
  default     = "dev"
}

provider "aws" {
  profile = var.aws_profile
  region  = var.aws_region

  default_tags {
    tags = {
      Project     = "mlops-platform-aws"
      Environment = var.environment
      ManagedBy   = "Terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  bucket_names = {
    datasets        = "mlops-aws-datasets-${var.environment}-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
    model_artifacts = "mlops-aws-model-artifacts-${var.environment}-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
    mlflow_backups  = "mlops-aws-mlflow-backups-${var.environment}-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
  }

  repository_names = {
    training  = "mlops-aws-training-${var.environment}"
    inference = "mlops-aws-inference-${var.environment}"
    mlflow    = "mlops-aws-mlflow-${var.environment}"
  }
}

resource "aws_s3_bucket" "persistent" {
  for_each      = local.bucket_names
  bucket        = each.value
  force_destroy = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "persistent" {
  for_each = aws_s3_bucket.persistent
  bucket   = each.value.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "persistent" {
  for_each = aws_s3_bucket.persistent
  bucket   = each.value.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "persistent" {
  for_each = aws_s3_bucket.persistent
  bucket   = each.value.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_ecr_repository" "images" {
  for_each             = local.repository_names
  name                 = each.value
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  lifecycle {
    prevent_destroy = true
  }
}
