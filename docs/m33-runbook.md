# M3.3 — CPU HPA và monitoring

Đã triển khai trên EKS `mlops-aws-dev` ngày 2026-10-23. [Bằng chứng thực tế](evidence/m33-2026-10-23.md) ghi các kết quả HPA, k6, Prometheus, Grafana và trạng thái hạ tầng. Các lệnh apply bên dưới là quy trình dựng lại hoặc thay đổi có review, không cần chạy lại để xem dashboard hiện tại.

## Kiến trúc và phạm vi

- EKS Metrics Server community add-on `v0.9.0-eksbuild.13` cung cấp CPU metrics cho Kubernetes HPA và `kubectl top`. Đây là metrics tức thời; Prometheus lưu lịch sử.
- Argo CD Application `inference-api` vẫn tự sync Deployment, Service và HPA. HPA `autoscaling/v2` đặt CPU target 60%, min 1, max 3, scale-down stabilization 300 giây. Application dùng `ignoreDifferences` và `RespectIgnoreDifferences=true` cho `/spec/replicas`, giữ `selfHeal=true`, `prune=false`.
- Helm release `m33-prometheus` (prometheus-community chart 29.37.1) chạy một Pod trên system node, scrape từng inference Pod trong namespace `mlops` qua Kubernetes Pod discovery và `/metrics`. RBAC chỉ đọc Pods trong `mlops`. Alertmanager, Pushgateway, kube-state-metrics, node-exporter và các scrape jobs mặc định đều tắt. Dữ liệu 24 giờ nằm trên `emptyDir` tối đa 1 GiB, sẽ mất khi Pod bị thay/xóa; đây không phải monitoring HA.
- Helm release `m33-grafana` (grafana-community chart 14.0.1) chạy trên system node. Datasource là `http://m33-prometheus-server.monitoring.svc.cluster.local:80`. Dashboard được provision từ JSON trong repository, Secret `m33-grafana-admin` được tạo lúc deploy, không đưa mật khẩu vào Git. Dashboard provider quét file ConfigMap mỗi 30 giây để nhận thay đổi; Grafana Deployment dùng `Recreate` vì system node không đủ CPU request cho hai Grafana Pods khi RollingUpdate. Việc nâng cấp Grafana có thể gián đoạn UI ngắn, không ảnh hưởng inference. Hai Services là ClusterIP, truy cập bằng port-forward.
- k6 Job giới hạn 1–50 VUs, 1–200 requests/s, 30–900 giây; request thật đến `/predict`. NodePool serving `inference-on-demand` và image digest vẫn nguyên. HPA tăng Pods; Karpenter chỉ tạo node nếu Pod mới không đặt được lên node hiện có.

## Trạng thái được đọc trước triển khai

Trước khi triển khai, `aws eks list-addons` cho thấy Metrics Server chưa được EKS quản lý. `aws eks describe-addon-versions --addon-name metrics-server --kubernetes-version 1.36` trả `v0.9.0-eksbuild.13` là bản mặc định. Sau triển khai, add-on đã `ACTIVE` với 2 Pods. EC2 vẫn có một `t3.large` system và một `t3.large` thuộc `inference-on-demand` (mỗi máy 2 vCPU/8 GiB). Deployment inference request 250m CPU/512Mi, limit 1 CPU/1Gi. CIDR public API có thể thay đổi khi IP egress của mạng quản trị đổi; luôn đọc và review các `/32` hiện có trước Terraform plan, không commit IP cá nhân hoặc mở `0.0.0.0/0`.

Metrics Server (2 Pods) + Prometheus + config reloader + Grafana yêu cầu tổng 410m CPU/1.05Gi trên system node; k6 khi chạy cần thêm 100m/128Mi. Hai inference Pods tăng thêm cần 500m CPU/1Gi theo requests, giới hạn tăng thêm 2 CPU/2Gi. NodePool inference giới hạn 4 vCPU; Karpenter có thể tạo thêm On-Demand node và phát sinh phí EC2/EBS nếu node hiện tại không đủ chỗ. Bài test 2026-10-23 dùng node inference đã có. Prometheus/Grafana không tạo PVC, ALB, NAT hay public endpoint mới.

## Review và kiểm tra offline (chưa thay AWS)

```bash
export AWS_PROFILE=mlops-dev AWS_REGION=ap-southeast-1
aws sts get-caller-identity
aws eks list-addons --cluster-name mlops-aws-dev
aws eks describe-addon-versions --addon-name metrics-server --kubernetes-version 1.36 \
  --query 'addons[0].addonVersions[0].addonVersion' --output text

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
helm repo add grafana-community https://grafana-community.github.io/helm-charts --force-update
helm repo update
make m33-test
terraform -chdir=infra/terraform/platform fmt -check
terraform -chdir=infra/terraform/platform validate

# Tạm truyền danh sách CIDR live đã review để plan không ghi đè IP quản trị.
# Nếu muốn thay đổi CIDR, chỉnh danh sách /32 này rõ ràng rồi review diff trước apply.
export TF_VAR_eks_public_access_cidrs="$(aws eks describe-cluster \
  --name mlops-aws-dev --query 'cluster.resourcesVpcConfig.publicAccessCidrs' --output json)"
terraform -chdir=infra/terraform/platform plan -out=/tmp/m33-platform.tfplan
terraform -chdir=infra/terraform/platform show /tmp/m33-platform.tfplan
```

Ở lần triển khai đầu, plan là **1 add** (`aws_eks_addon.metrics_server`), **0 change, 0 destroy**. Với cluster đã triển khai, plan hiện kỳ vọng **No changes**. Nếu Metrics Server tồn tại ngoài Terraform khi dựng lại cluster, giải quyết ownership trước khi apply. Kiểm tra `kubectl -n kube-system get deployment metrics-server` từ IP được cấp quyền. Nếu plan có thay đổi CIDR/VPC/EKS khác ngoài dự kiến, dừng để điều tra drift.

## Thứ tự triển khai sau khi review

Các lệnh sau đã được thực hiện khi triển khai M3.3; chỉ chạy lại khi dựng lại cluster hoặc thay đổi cấu hình đã được review, từ máy có EKS API access:

```bash
terraform -chdir=infra/terraform/platform apply /tmp/m33-platform.tfplan
aws eks update-kubeconfig --profile mlops-dev --region ap-southeast-1 --name mlops-aws-dev
kubectl -n kube-system rollout status deployment/metrics-server --timeout=5m
kubectl top nodes
kubectl -n mlops top pods -l app=inference-api

# Cập nhật Application đang chạy trước khi HPA xuất hiện trong GitOps path.
kubectl apply -f deploy/argocd/inference-application-automated.yaml
kubectl -n argocd get application inference-api -o yaml | rg -A8 'ignoreDifferences:'

# Commit/push source M3.3 đã review. Argo CD sẽ tự tạo HPA; không kubectl apply HPA.
git add Makefile README.md docs/m33-runbook.md infra/terraform/platform/eks.tf \
  deploy/argocd/inference-application-automated.yaml deploy/gitops/inference \
  deploy/monitoring scripts/m33.sh scripts/m32_adoption.py tests/unit/test_m33_config.py
git commit -m 'm33: add inference HPA and monitoring'
git push origin main
kubectl -n argocd get application inference-api -o wide
kubectl -n mlops get hpa inference-api -o wide

# Mật khẩu chỉ ở môi trường shell local; Secret được tạo trong cluster.
read -rsp 'Grafana admin password: ' GRAFANA_ADMIN_PASSWORD; echo
export GRAFANA_ADMIN_PASSWORD
make m33-deploy
unset GRAFANA_ADMIN_PASSWORD
make m33-verify
```

Nếu `main` có branch protection, tạo PR chứa thay đổi M3.3 và merge theo quy trình; sau merge xác nhận Application sync. Workflow M3.2 release vẫn là luồng duy nhất để cập nhật serving image digest. Thay đổi Application M3.3 cần `kubectl apply` một lần vì Application này không được quản lý bởi parent Application.

## Kiểm chứng monitoring và HPA

`make m33-verify` kiểm tra Metrics Server, HPA, CPU Pod, NodeClaims, Helm releases, Prometheus targets `UP` và Grafana datasource. Để đọc số liệu trực tiếp ở hai terminal:

```bash
kubectl -n monitoring port-forward service/m33-prometheus-server 9090:80
kubectl -n monitoring port-forward service/m33-grafana 3000:80
```

Prometheus: `http://127.0.0.1:9090/targets` phải có `inference-api` với tất cả inference Pods đang Running ở trạng thái `UP`. Query trong UI: `up{job="inference-api"}`, `sum(rate(inference_http_requests_total{path="/predict"}[5m]))`, `histogram_quantile(0.95, sum by (le) (rate(inference_http_request_duration_seconds_bucket{path="/predict"}[5m])))`. Grafana: `http://127.0.0.1:3000`, dashboard **MLOps / Inference API**; kiểm tra datasource Prometheus và bốn panel. Pod status panel biểu thị khả năng scrape (`up`), không thay thế Kubernetes Ready condition.

Trước load test, đọc `kubectl -n mlops get hpa inference-api -o wide`: CPU current/target phải có giá trị, không phải `<unknown>`. Target 60% của request 250m tương ứng khoảng 150m CPU trung bình/Pod; đây là tỷ lệ utilization, không phải 60% của CPU limit. Theo dõi đồng thời:

```bash
kubectl -n mlops get hpa inference-api -w
kubectl -n mlops get deployment inference-api -w
kubectl get nodeclaims -w
```

Ở terminal khác, tăng tải từng bước; chạy 5–10 phút để HPA có đủ mẫu. Mỗi lần gọi tạo một k6 Job có `generateName` riêng:

```bash
make m33-load-test M33_VUS=10 M33_RATE=20 M33_DURATION_SECONDS=300
make m33-load-test M33_VUS=30 M33_RATE=100 M33_DURATION_SECONDS=600
# Chỉ tăng đến 200 rps/50 VUs nếu cluster vẫn khỏe và chưa có 429/5xx kéo dài.
```

Kỳ vọng: k6 log có HTTP 200, `kubectl top` có CPU, HPA event `SuccessfulRescale` và Deployment tăng ít nhất 2 replicas nếu inference thực sự dùng đủ CPU. `kubectl -n mlops describe hpa inference-api` cho current/target và events. Nếu CPU không đạt ngưỡng, ghi nhận giới hạn bài test; không sửa model hay tạo endpoint đốt CPU. Nếu Pod Pending, `kubectl -n mlops describe pod <name>` và `kubectl get nodeclaims` cho biết Karpenter có tạo node hay vướng giới hạn 4 vCPU. Sau khi dừng tải, đợi ít nhất 5 phút cộng thời gian HPA sync, xác nhận trở về 1 replica; nếu vẫn >1, kiểm tra CPU nền và HPA events. Dashboard dùng cửa sổ rate 5 phút nên một số panel cần vài phút để có đường biểu diễn.

## Troubleshooting và cleanup

- `kubectl` timeout: kiểm tra IP public hiện tại có nằm trong `publicAccessCidrs` của EKS và kubeconfig đúng cluster; không mở `0.0.0.0/0`.
- HPA `<unknown>`: kiểm tra Metrics Server add-on health, `kubectl top pods`, CPU request và `kubectl -n kube-system logs deployment/metrics-server`.
- Prometheus target `DOWN` hoặc không có target: kiểm tra Pod labels/port `http`, `kubectl -n mlops get rolebinding m33-inference-pod-reader`, Prometheus `/targets` và log. Prometheus chỉ được list/watch Pods namespace `mlops`.
- Grafana không có dashboard: kiểm tra ConfigMap `m33-inference-dashboard`, Helm values/provisioning và Pod logs. Sai login: kiểm tra Secret tồn tại, đặt lại qua `make m33-deploy` với mật khẩu mới rồi restart Grafana Deployment nếu cần.
- Quá tải system node: giảm k6 rate hoặc dừng Job; Prometheus/Grafana có thể cần capacity system node bổ sung sau khi đo requests thực tế. Nếu Grafana upgrade Pending với `Insufficient cpu`, kiểm tra chart values vẫn có `deploymentStrategy.type: Recreate`.

`make m33-cleanup` chỉ gỡ hai Helm releases, RBAC/Secret/ConfigMaps monitoring và k6 Jobs/ConfigMap; không xóa serving workload, MLflow, PostgreSQL, PVC, Argo CD hay Metrics Server. HPA là GitOps resource nên nếu muốn ngừng HPA, phải review một Git change gỡ `hpa.yaml` và điều chỉnh Application replica ownership riêng. Không dùng `m31-cleanup` khi Argo CD đang quản lý serving.
