# Phase 3 — Automated Deployment, Serving & Observability

## 1. Mục tiêu
Đưa approved Registry model vào API có phiên bản rõ ràng, triển khai bằng GitOps, kiểm chứng trước champion promotion, rồi quan sát và autoscale theo CPU.

## 2. Scope
FastAPI, model export/bundle, Docker/ECR, GitHub Actions OIDC, Argo CD, HPA, Metrics Server, Prometheus/Grafana và k6. Không bao gồm canary, metric-based rollback hoặc retraining.

## 3. Kiến trúc và thành phần
`src/inference/` export và validate release bundle, tái sử dụng `training.features.preprocess_request`. `deploy/inference/Dockerfile` đóng model vào image. GitHub workflow nhận explicit Registry name/version, dùng IAM OIDC role, EKS Access Entry và namespace RBAC; Argo CD đọc `deploy/gitops/inference`. HPA CPU target 60%, min1/max3 và sở hữu Deployment replicas; Application dùng `ignoreDifferences`/`RespectIgnoreDifferences=true`. Prometheus scrape Pod `/metrics` và Grafana dùng Service DNS.

## 4. Đã triển khai
FastAPI `inference-api` ở namespace `mlops`, Service ClusterIP port 8000 và image pin bằng digest. Registry approved model version 1 được phát hành qua GitHub Actions và alias `champion` chỉ được xác nhận sau verification. Argo CD Application tự sync, `selfHeal=true` và `prune=false`. Metrics Server, HPA, Prometheus, Grafana và load test đã chạy trên dev; không có public ALB/Ingress.

## 5. Luồng hoạt động
Người vận hành dispatch release version rõ ràng → runner OIDC → tạm cấp runner `/32` vào EKS API → localhost tunnel tới cloud MLflow → approval/schema/checksum → bundle → ECR image digest → **một GitOps commit** → Argo CD sync → Deployment/Pod/API checks → idempotent champion promotion → khôi phục CIDRs. Request API có timestamp timezone; chuyển sang America/New_York, tạo đúng năm features và dự đoán từ model load sẵn. HPA đọc CPU từ Metrics Server; Prometheus/Grafana hiển thị rate, latency, error, Pod scrape status.

## 6. Cấu hình và thực thi
Serving lần đầu cần `make serving-export`/`serving-build`/`serving-push` và review Application/manual adoption. Sau đó `make release-run M31_MODEL_NAME=<name> M31_MODEL_VERSION=<version>` là đường phát hành duy nhất. `make monitoring-deploy` cần Grafana password qua environment; values/chart versions ở `deploy/monitoring/`. [Setup](../setup.md) ghi thứ tự cùng điều kiện OIDC/account identity.

## 7. Kiểm chứng
`make release-test`, `make release-gitops-verify`, `make monitoring-test`, `make monitoring-verify`; thử `/readyz`, `/model-info`, `/predict` qua port-forward. HPA phải có current/target CPU. `make monitoring-load-test` tạo k6 Job trong giới hạn; quan sát `kubectl get hpa -w`, Pods và NodeClaims. Không dispatch release chỉ để kiểm tra monitoring.

## 8. Kết quả và bằng chứng
[Evidence Phase 3](../evidence/phase-03.md) nêu release digest/commit, API response, CI, Prometheus/Grafana và bài k6 55.398 requests, 0 HTTP failure, HPA 1→3→1. [CSV HPA](../evidence/phase-03-hpa-samples.csv) giữ mẫu CPU/replica theo thời gian.

## 9. Hạn chế vận hành
Release runner phụ thuộc EKS public API `/32` tạm thời; cleanup bị gián đoạn cần recovery CIDR có kiểm soát. Image-baked model cần release mới khi model mới được duyệt. Prometheus metrics ephemeral 24 giờ; Grafana `Recreate` có thể gián đoạn UI ngắn khi upgrade. Bài k6 chứng minh scaling/availability, chưa chứng minh throughput tăng sau scale-out.
