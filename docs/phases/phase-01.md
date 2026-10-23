# Phase 1 — AWS Infrastructure Foundation

## 1. Mục tiêu
Tạo nền tảng có thể quản lý bằng IaC, phân biệt tài nguyên phải giữ lâu dài với cluster/dev workloads có thể dừng và dựng lại.

## 2. Scope
Terraform bootstrap, persistent và platform; VPC/EKS, managed system node, Karpenter, S3/ECR/IAM, Argo CD; lifecycle verification và cleanup. Phase này chưa train hoặc serve model.

## 3. Kiến trúc và thành phần
`infra/terraform/bootstrap` tạo state bucket bằng local Terraform state. `persistent` tạo S3 Datasets, Model Artifacts, PostgreSQL Backups và ECR training/inference/MLflow; S3 backend state riêng. `platform` tạo VPC với public/private subnets tại hai AZ, một NAT Gateway, S3 Gateway Endpoint, EKS, system managed node group, IAM/Pod Identity và add-ons. Karpenter có `training-spot` và `inference-on-demand` NodePools. Argo CD được cài bằng Helm, chỉ có ClusterIP.

## 4. Đã triển khai
Môi trường dev `mlops-aws-dev` ở `ap-southeast-1` chạy Kubernetes 1.36. EKS public/private endpoints bật, public API chỉ nhận danh sách admin `/32`. Metrics Server và EBS CSI là EKS add-ons; Karpenter và Argo CD chạy trên system nodes. Tên, state addresses và resource identities được giữ nguyên trong đợt chuẩn hóa repository.

## 5. Luồng hoạt động
Terraform bootstrap → persistent → platform. Sau khi kubeconfig truy cập được EKS, `make k8s-bootstrap` cài Helm charts, EC2NodeClass và NodePools. Pod có selector/toleration sẽ kích hoạt Karpenter tạo đúng loại node. GitOps Application có source Git, namespace và sync policy riêng.

## 6. Cấu hình và thực thi
Theo [setup](../setup.md): `make tf-bootstrap-init`, `make tf-bootstrap-plan`, `make tf-bootstrap-apply`; tiếp đến nhóm `tf-persistent-*`, rồi cấp `TF_VAR_eks_public_access_cidrs` và nhóm `tf-platform-*`. Cài system components bằng `make k8s-bootstrap`. Giữ bootstrap state backup ngoài Git. Account cần EC2 Spot service-linked role trước lần provision Spot.

## 7. Kiểm chứng
`make tf-platform-verify` và `make k8s-verify` kiểm tra EKS/nodes/system pods. `make k8s-smoke` tạo Job Spot có thể phát sinh EC2/EBS phí; chỉ chạy khi muốn kiểm thử provisioning. `kubectl get nodepools,nodeclaims` kiểm tra Karpenter. Terraform plan cần được review trước apply hoặc destroy.

## 8. Kết quả và bằng chứng
[Evidence Phase 1](../evidence/phase-01.md) ghi cluster/add-on/NodeClaim và Terraform no-change plan lần kiểm tra dev cuối. Đây chưa phải bằng chứng dựng lại toàn bộ trên account mới.

## 9. Hạn chế vận hành
Một NAT, một system node ban đầu và EBS theo AZ chưa HA. Mạng quản trị có IP egress thay đổi; cập nhật CIDRs an toàn qua Terraform, không mở toàn Internet. `make tf-platform-down` chỉ dành cho shutdown đã backup, có xác nhận; persistent S3/ECR và bootstrap state cần được giữ.
