# AWS identities only. Workloads and the gp3 StorageClass live in deploy/platform.
data "aws_eks_addon_version" "ebs_csi" {
  addon_name         = "aws-ebs-csi-driver"
  kubernetes_version = "1.36"
}

locals {
  mlops_bucket_names = {
    datasets        = "mlops-aws-datasets-${var.environment}-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
    model_artifacts = "mlops-aws-model-artifacts-${var.environment}-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
    mlflow_backups  = "mlops-aws-mlflow-backups-${var.environment}-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
  }
  mlops_workloads = {
    mlflow = {
      service_account = "mlflow"
      bucket          = local.mlops_bucket_names.model_artifacts
      prefix          = "mlflow/"
    }
    training = {
      service_account = "training"
      bucket          = local.mlops_bucket_names.datasets
      prefix          = "yellow-taxi/"
    }
    backup = {
      service_account = "mlflow-backup"
      bucket          = local.mlops_bucket_names.mlflow_backups
      prefix          = "postgres/"
    }
  }
}

# Reads confirm the persistent buckets already exist; this stack never owns them.
data "aws_s3_bucket" "mlops" {
  for_each = local.mlops_bucket_names
  bucket   = each.value
}

data "aws_iam_policy_document" "ebs_csi_trust" {
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
      values   = ["kube-system"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes-service-account"
      values   = ["ebs-csi-controller-sa"]
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  name               = "${local.cluster_name}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.ebs_csi_trust.json
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEBSCSIDriverEKSClusterScopedPolicy"
}

resource "aws_eks_addon" "ebs_csi" {
  cluster_name  = module.eks.cluster_name
  addon_name    = "aws-ebs-csi-driver"
  addon_version = data.aws_eks_addon_version.ebs_csi.version

  pod_identity_association {
    service_account = "ebs-csi-controller-sa"
    role_arn        = aws_iam_role.ebs_csi.arn
  }

  # Controller pods need a Ready system node; otherwise a fresh add-on can stay
  # DEGRADED until its creation timeout even though EKS later recovers it.
  depends_on = [aws_iam_role_policy_attachment.ebs_csi, module.eks]
}

data "aws_iam_policy_document" "mlops_trust" {
  for_each = local.mlops_workloads
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
      values   = [each.value.service_account]
    }
  }
}

resource "aws_iam_role" "mlops" {
  for_each           = local.mlops_workloads
  name               = "${local.cluster_name}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.mlops_trust[each.key].json
}

data "aws_iam_policy_document" "mlops_s3" {
  for_each = local.mlops_workloads
  statement {
    sid       = "ListOwnPrefix"
    actions   = ["s3:ListBucket"]
    resources = [data.aws_s3_bucket.mlops[each.key == "mlflow" ? "model_artifacts" : each.key == "backup" ? "mlflow_backups" : "datasets"].arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = [trimsuffix(each.value.prefix, "/"), "${each.value.prefix}*"]
    }
  }
  statement {
    sid       = "ReadOwnObjects"
    actions   = ["s3:GetObject"]
    resources = ["${data.aws_s3_bucket.mlops[each.key == "mlflow" ? "model_artifacts" : each.key == "backup" ? "mlflow_backups" : "datasets"].arn}/${each.value.prefix}*"]
  }
  dynamic "statement" {
    for_each = each.key == "training" ? [] : [1]
    content {
      sid       = "WriteOwnObjects"
      actions   = ["s3:PutObject", "s3:AbortMultipartUpload"]
      resources = ["${data.aws_s3_bucket.mlops[each.key == "mlflow" ? "model_artifacts" : "mlflow_backups"].arn}/${each.value.prefix}*"]
    }
  }
}

resource "aws_iam_role_policy" "mlops_s3" {
  for_each = local.mlops_workloads
  name     = "s3-${each.key}"
  role     = aws_iam_role.mlops[each.key].id
  policy   = data.aws_iam_policy_document.mlops_s3[each.key].json
}

resource "aws_eks_pod_identity_association" "mlops" {
  for_each        = local.mlops_workloads
  cluster_name    = module.eks.cluster_name
  namespace       = "mlops"
  service_account = each.value.service_account
  role_arn        = aws_iam_role.mlops[each.key].arn
}
