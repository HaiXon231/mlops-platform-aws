# The GitHub provider already exists in this account. Never create a second one.
data "aws_iam_openid_connect_provider" "github_actions" {
  url = "https://token.actions.githubusercontent.com"
}

locals {
  m32_bucket_arn = data.aws_s3_bucket.mlops["model_artifacts"].arn
  m32_ecr_arn    = "arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/mlops-aws-inference-${var.environment}"
  # GitHub repository currently uses immutable OIDC subjects. Verified by its
  # OIDC customization API: owner 204581707, repository 1424395869.
  m32_github_subject = "repo:HaiXon231@204581707/mlops-platform-aws@1424395869:ref:refs/heads/main"
}

data "aws_iam_policy_document" "m32_github_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github_actions.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.m32_github_subject]
    }
  }
}

resource "aws_iam_role" "m32_github_release" {
  name               = "${local.cluster_name}-github-release"
  assume_role_policy = data.aws_iam_policy_document.m32_github_trust.json
}

data "aws_iam_policy_document" "m32_github_release" {
  statement {
    sid       = "ListReleasePrefix"
    actions   = ["s3:ListBucket"]
    resources = [local.m32_bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["releases/m32/*"]
    }
  }
  statement {
    sid       = "ReadReleaseBundleAndStatus"
    actions   = ["s3:GetObject"]
    resources = ["${local.m32_bucket_arn}/releases/m32/*"]
  }
  statement {
    sid       = "GetEcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "PushAndInspectInferenceImage"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload", "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart", "ecr:PutImage", "ecr:DescribeImages", "ecr:BatchGetImage"
    ]
    resources = [local.m32_ecr_arn]
  }
}

resource "aws_iam_role_policy" "m32_github_release" {
  name   = "release-s3-read-ecr-push"
  role   = aws_iam_role.m32_github_release.id
  policy = data.aws_iam_policy_document.m32_github_release.json
}

data "aws_iam_policy_document" "m32_pod_trust" {
  for_each = toset(["export", "verify"])
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/eks-cluster-name"
      values   = [local.cluster_name]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes-namespace"
      values   = ["mlops"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes-service-account"
      values   = ["inference-release-${each.key}"]
    }
  }
}

resource "aws_iam_role" "m32_pod" {
  for_each           = toset(["export", "verify"])
  name               = "${local.cluster_name}-release-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.m32_pod_trust[each.key].json
}

data "aws_iam_policy_document" "m32_export_s3" {
  statement {
    sid       = "ListMlflowAndReleaseObjects"
    actions   = ["s3:ListBucket"]
    resources = [local.m32_bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["mlflow/*", "releases/m32/*"]
    }
  }
  statement {
    sid       = "ReadMlflowAndReleaseObjects"
    actions   = ["s3:GetObject"]
    resources = ["${local.m32_bucket_arn}/mlflow/*", "${local.m32_bucket_arn}/releases/m32/*"]
  }
  statement {
    sid       = "WriteReleaseBundle"
    actions   = ["s3:PutObject", "s3:AbortMultipartUpload"]
    resources = ["${local.m32_bucket_arn}/releases/m32/*"]
  }
}

data "aws_iam_policy_document" "m32_verify_s3" {
  statement {
    sid     = "WriteReleaseVerificationStatus"
    actions = ["s3:PutObject"]
    resources = [
      "${local.m32_bucket_arn}/releases/m32/*/verified.json",
      "${local.m32_bucket_arn}/releases/m32/*/failed.json"
    ]
  }
}

resource "aws_iam_role_policy" "m32_export_s3" {
  name   = "approved-model-export"
  role   = aws_iam_role.m32_pod["export"].id
  policy = data.aws_iam_policy_document.m32_export_s3.json
}

resource "aws_iam_role_policy" "m32_verify_s3" {
  name   = "release-verification-status"
  role   = aws_iam_role.m32_pod["verify"].id
  policy = data.aws_iam_policy_document.m32_verify_s3.json
}

resource "aws_eks_pod_identity_association" "m32_release" {
  for_each = {
    export = "inference-release-export"
    verify = "inference-release-verify"
  }
  cluster_name    = module.eks.cluster_name
  namespace       = "mlops"
  service_account = each.value
  role_arn        = aws_iam_role.m32_pod[each.key].arn
}
