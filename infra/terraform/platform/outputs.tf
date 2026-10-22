output "vpc_id" {
  description = "Platform VPC ID."
  value       = module.vpc.vpc_id
}

output "public_subnet_ids" {
  description = "Public subnet IDs for future internet-facing ALBs."
  value       = module.vpc.public_subnets
}

output "private_subnet_ids" {
  description = "Private subnet IDs for EKS nodes and future Karpenter nodes."
  value       = module.vpc.private_subnets
}

output "private_route_table_ids" {
  description = "Private route tables used by the S3 Gateway Endpoint."
  value       = module.vpc.private_route_table_ids
}

output "s3_gateway_endpoint_id" {
  description = "S3 Gateway VPC Endpoint ID."
  value       = aws_vpc_endpoint.s3.id
}

output "eks_cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "eks_cluster_endpoint" {
  description = "Kubernetes API endpoint."
  value       = module.eks.cluster_endpoint
}

output "eks_cluster_admin_principal_arn" {
  description = "IAM principal mapped to cluster admin by EKS Access Entry."
  value       = data.aws_iam_session_context.current.issuer_arn
}

output "managed_node_group_name" {
  description = "EKS managed node group name."
  value       = split(":", module.eks.eks_managed_node_groups["system"].node_group_id)[1]
}

output "managed_node_group_role_arn" {
  description = "System managed node group IAM role ARN."
  value       = module.eks.eks_managed_node_groups["system"].iam_role_arn
}

output "node_security_group_id" {
  description = "Shared node security group tagged for future Karpenter discovery."
  value       = module.eks.node_security_group_id
}

output "cluster_primary_security_group_id" {
  description = "EKS-created primary cluster security group."
  value       = module.eks.cluster_primary_security_group_id
}

output "oidc_provider_arn" {
  description = "IAM OIDC provider ARN for future IRSA roles."
  value       = module.eks.oidc_provider_arn
}

output "oidc_issuer_url" {
  description = "EKS OIDC issuer URL for future IRSA roles."
  value       = module.eks.cluster_oidc_issuer_url
}

output "karpenter_node_role_name" {
  description = "IAM role attached to the Karpenter node instance profile."
  value       = module.karpenter.node_iam_role_name
}

output "karpenter_node_instance_profile_name" {
  description = "Terraform-managed IAM instance profile named in the Karpenter EC2NodeClass."
  value       = module.karpenter.instance_profile_name
}

output "karpenter_controller_role_arn" {
  description = "IAM role associated with Karpenter's service account through EKS Pod Identity."
  value       = module.karpenter.iam_role_arn
}

output "karpenter_interruption_queue_name" {
  description = "SQS queue consumed by Karpenter for Spot and maintenance events."
  value       = module.karpenter.queue_name
}

output "ebs_csi_addon_version" {
  description = "EKS default EBS CSI add-on version compatible with Kubernetes 1.36."
  value       = data.aws_eks_addon_version.ebs_csi.version
}

output "mlops_pod_identity_role_arns" {
  description = "Workload roles for MLflow, training and PostgreSQL backup."
  value       = { for purpose, role in aws_iam_role.mlops : purpose => role.arn }
}

output "m32_github_release_role_arn" {
  description = "OIDC role assumed only by the immutable GitHub main-branch subject."
  value       = aws_iam_role.m32_github_release.arn
}

output "m32_github_release_access_group" {
  description = "Kubernetes RBAC group granted to the GitHub release role through its EKS Access Entry."
  value       = "mlops-github-release"
}

output "m32_release_pod_identity_role_arns" {
  description = "In-cluster export and verification roles."
  value       = { for purpose, role in aws_iam_role.m32_pod : purpose => role.arn }
}
