# AWS-side Karpenter integration. Kubernetes CRDs and Helm releases live in deploy/.
module "karpenter" {
  source  = "terraform-aws-modules/eks/aws//modules/karpenter"
  version = "21.26.0"

  enable_inline_policy = true

  cluster_name = module.eks.cluster_name

  create_pod_identity_association = true
  namespace                       = "kube-system"
  service_account                 = "karpenter"

  enable_spot_termination   = true
  queue_name                = "Karpenter-${local.cluster_name}"
  queue_managed_sse_enabled = true

  node_iam_role_name            = "Karpenter-${local.cluster_name}"
  node_iam_role_use_name_prefix = false
  create_instance_profile       = true
  create_access_entry           = true
  access_entry_type             = "EC2_LINUX"

  tags = {
    Project     = "mlops-platform-aws"
    Environment = var.environment
    ManagedBy   = "Terraform"
  }
}
