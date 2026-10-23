# Phase 1 — bằng chứng hạ tầng

Lần kiểm tra EKS được ghi nhận ngày **2026-10-23** tại `ap-southeast-1`: cluster `mlops-aws-dev` ACTIVE, Kubernetes 1.36; public và private API endpoints cùng bật, public access bị giới hạn bởi năm CIDR `/32`. `kubectl` truy cập được cluster. EKS Metrics Server add-on `v0.9.0-eksbuild.13` ACTIVE với 2 Pods Ready; đây là bổ sung sau nền tảng ban đầu.

Terraform tách bootstrap local state, persistent S3 state và platform S3 state. Lần platform plan gần nhất với CIDRs live được review báo `No changes`; không có apply trong phiên đó. Đây là bằng chứng **không có drift ở plan đó**, không phải bằng chứng toàn bộ bootstrap đã được thực hiện lại từ đầu. Persistent buckets/ECR và EKS/NodePool đang phục vụ các Phase sau. Bài load test có một NodeClaim `inference-on-demand` và hai EC2 `t3.large` đang chạy (system + inference), không tạo node mới. Karpenter Spot training đã được kiểm chứng ở Phase 2; xem [Phase 2](phase-02.md).

Kiểm tra lặp lại không thay đổi tài nguyên: `make tf-platform-verify`, `make k8s-verify`, `kubectl get nodepools,nodeclaims` và Terraform plan với CIDRs đã review. Không commit bootstrap state hoặc IP quản trị.
