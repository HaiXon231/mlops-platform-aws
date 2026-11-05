# AWS Load Balancer Controller (Phase 4): chỉ phần IAM/Pod Identity. Helm release nằm ở scripts/m4x.
# Policy là bản chính thức của kubernetes-sigs/aws-load-balancer-controller, pin theo tag.
locals {
  alb_controller_service_account = "aws-load-balancer-controller"
}

resource "aws_iam_policy" "alb_controller" {
  name        = "${local.cluster_name}-aws-load-balancer-controller"
  description = "Official AWS Load Balancer Controller v3.6.2 policy."
  policy      = file("${path.module}/policies/aws-load-balancer-controller-v3.6.2.json")
}

data "aws_iam_policy_document" "alb_controller_trust" {
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
      values   = [local.alb_controller_service_account]
    }
  }
}

resource "aws_iam_role" "alb_controller" {
  name               = "${local.cluster_name}-alb-controller"
  assume_role_policy = data.aws_iam_policy_document.alb_controller_trust.json
}

resource "aws_iam_role_policy_attachment" "alb_controller" {
  role       = aws_iam_role.alb_controller.name
  policy_arn = aws_iam_policy.alb_controller.arn
}

resource "aws_eks_pod_identity_association" "alb_controller" {
  cluster_name    = module.eks.cluster_name
  namespace       = "kube-system"
  service_account = local.alb_controller_service_account
  role_arn        = aws_iam_role.alb_controller.arn

  depends_on = [aws_iam_role_policy_attachment.alb_controller]
}
