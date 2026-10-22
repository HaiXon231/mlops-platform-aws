module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "21.26.0"

  name                                   = local.cluster_name
  kubernetes_version                     = "1.36"
  enabled_log_types                      = ["api", "audit", "authenticator"]
  cloudwatch_log_group_retention_in_days = 7

  # EKS Standard control plane; no Auto Mode, Fargate, or customer-managed KMS key.
  authentication_mode                      = "API"
  enable_cluster_creator_admin_permissions = false
  create_kms_key                           = false
  encryption_config                        = null
  enable_irsa                              = true

  endpoint_private_access      = true
  endpoint_public_access       = true
  endpoint_public_access_cidrs = var.eks_public_access_cidrs

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # Only this shared node SG receives the Karpenter discovery tag.
  node_security_group_tags = {
    "karpenter.sh/discovery" = local.cluster_name
  }

  access_entries = {
    lab_admin = {
      principal_arn = data.aws_iam_session_context.current.issuer_arn
      policy_associations = {
        cluster_admin = {
          policy_arn = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          access_scope = {
            type = "cluster"
          }
        }
      }
    }
    github_release = {
      principal_arn     = aws_iam_role.m32_github_release.arn
      kubernetes_groups = ["mlops-github-release"]
    }
  }

  addons = {
    vpc-cni = {
      before_compute = true
    }
    coredns                = {}
    kube-proxy             = {}
    eks-pod-identity-agent = {}
  }

  eks_managed_node_groups = {
    system = {
      name            = "system"
      use_name_prefix = false
      subnet_ids      = module.vpc.private_subnets

      ami_type                   = "AL2023_x86_64_STANDARD"
      instance_types             = ["t3.large"]
      capacity_type              = "ON_DEMAND"
      create_launch_template     = true
      use_custom_launch_template = true

      min_size     = 1
      desired_size = 1
      max_size     = 2

      # Custom launch template is required to set an encrypted gp3 root volume.
      block_device_mappings = {
        root = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size           = 30
            volume_type           = "gp3"
            encrypted             = true
            delete_on_termination = true
          }
        }
      }

      metadata_options = {
        http_endpoint               = "enabled"
        http_tokens                 = "required"
        http_put_response_hop_limit = 2
      }

      # Core VPC CNI runs on the node role until a dedicated pod role is added.
      iam_role_attach_cni_policy = true
      create_security_group      = false
    }
  }

}
