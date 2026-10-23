# Kiến trúc hệ thống

[README](../README.md) có sơ đồ tổng quan; tài liệu này mô tả ranh giới sở hữu và ba luồng đang vận hành.

## Tài nguyên và quyền sở hữu

| Lớp | State / nơi khai báo | Vòng đời |
| --- | --- | --- |
| Bootstrap | `infra/terraform/bootstrap`, state local | Bucket Terraform state; sao lưu state ngoài Git |
| Persistent | `infra/terraform/persistent`, state S3 riêng | S3 datasets, model artifacts, backups và ECR; giữ khi dừng platform |
| Platform | `infra/terraform/platform`, state S3 riêng | VPC, EKS, node group system, IAM, add-ons và Pod Identity |
| Kubernetes nền | `deploy/platform`, `deploy/bootstrap` | Karpenter, Argo CD, MLflow, PostgreSQL/PVC, namespaces và RBAC |
| Serving GitOps | `deploy/gitops/inference` | Argo CD quản lý Deployment, Service, HPA; image được pin digest |
| Monitoring | `deploy/monitoring` | Helm Prometheus/Grafana, k6 Job; chỉ ClusterIP |

Karpenter dùng Spot NodePool `training-spot` cho Job huấn luyện và On-Demand NodePool `inference-on-demand` cho FastAPI. Managed Node Group `system` chạy control workloads, MLflow, PostgreSQL và monitoring. Các tên này là định danh runtime, không đổi khi dọn repository.

## Luồng dữ liệu và model

NYC TLC Parquet được tải vào local cache với SHA256/metadata rồi sync lên S3 Datasets. Job trên Spot tải đúng input, chạy cùng pipeline `src/training/` như local, ghi metrics/schema/manifest và log run vào MLflow. PostgreSQL giữ Tracking/Registry metadata; MLflow proxy artifact lên S3 Model Artifacts. Validation gate chỉ chấp thuận candidate nếu so sánh được trên cùng validation sample; test MAE chỉ báo cáo. Approved version là đầu vào release, không tự động chọn latest.

## Luồng phát hành

`workflow_dispatch` trên `main` nhận model name/version. GitHub OIDC lấy AWS role tạm thời. Runner tạm thêm đúng public IPv4 `/32` của mình vào EKS API CIDRs, dùng `kubectl port-forward` tới cloud MLflow, kiểm tra version READY/approved và tải artifacts. Bundle có model, schema, provenance và checksum; serving image được build/push lên ECR. Runner commit một lần cập nhật digest/metadata trong GitOps Deployment. Argo CD sync tự động; runner so actual Pod imageID, readiness và prediction, rồi mới xác nhận alias `champion`. Bước `always()` khôi phục CIDRs gốc. Thất bại ở verification không promote và không tự rollback traffic.

Argo CD Application `inference-api` theo dõi chính xác `deploy/gitops/inference`. HPA sở hữu `spec.replicas`; Application bỏ qua trường đó và dùng `RespectIgnoreDifferences=true`, `selfHeal=true`, `prune=false`.

## Luồng request và quan sát

Service `inference-api` là ClusterIP port 8000. FastAPI validate input và timezone, dùng `training.features.preprocess_request` để tạo đúng năm features và LightGBM categorical dtypes, trả thời lượng phút cùng version. Model load một lần khi process bắt đầu; image không phụ thuộc MLflow/S3 trên mỗi request. `/metrics` xuất request count, latency và error metrics với labels cardinality thấp. Metrics Server cấp CPU cho HPA (1–3 Pods, target 60% CPU request). Prometheus scrape từng inference Pod, giữ dữ liệu 24 giờ trên `emptyDir`; Grafana lấy datasource qua Service DNS. Không có public inference/MLflow/monitoring Ingress.

## Phạm vi chưa có

Phase 4 chưa có ALB/Argo Rollouts/automated rollback. Phase 5 chưa có drift detection hoặc retraining trigger. Một replica PostgreSQL, một NAT và metrics ephemeral chưa đáp ứng HA.
