# Vận hành và kiểm chứng

Chạy tại repository root với `AWS_PROFILE` đúng account, `AWS_REGION=ap-southeast-1` và kubeconfig trỏ đến cluster cần kiểm tra. `make help` mô tả các targets; [setup](setup.md) là trình tự dựng mới. Lệnh port-forward chiếm terminal cho đến khi nhấn Ctrl-C.

## Kiểm tra trạng thái thường ngày

```bash
aws sts get-caller-identity
aws eks describe-cluster --name mlops-aws-dev --query 'cluster.{status:status,cidrs:resourcesVpcConfig.publicAccessCidrs}'
kubectl -n argocd get application inference-api -o wide
kubectl -n mlops get deployment/inference-api deployment/mlflow statefulset/postgres
kubectl -n mlops get hpa inference-api -o wide
kubectl -n mlops top pods -l app=inference-api
kubectl get nodeclaims
make cloud-verify
make monitoring-verify
```

Nếu `kubectl` timeout, so IP egress hiện tại với CIDRs của EKS và kiểm tra kubeconfig; IP có thể đổi giữa hai lần gọi. Đừng mở `0.0.0.0/0`. Trước Terraform plan, truyền danh sách CIDRs live đã review để tránh ghi đè thay đổi quản trị: `export TF_VAR_eks_public_access_cidrs="$(aws eks describe-cluster --name mlops-aws-dev --query 'cluster.resourcesVpcConfig.publicAccessCidrs' --output json)"`. Nếu cần thay CIDR, review cả CIDRs đang có và plan trước apply.

## FastAPI và model

Terminal 1: `make serving-port-forward`. Terminal 2:

```bash
curl -fsS http://127.0.0.1:8000/readyz
curl -fsS http://127.0.0.1:8000/model-info
curl -fsS -H 'Content-Type: application/json' \
  -d '{"pickup_location_id":1,"dropoff_location_id":2,"pickup_datetime":"2025-04-07T16:00:00Z"}' \
  http://127.0.0.1:8000/predict
curl -fsS http://127.0.0.1:8000/metrics
```

`http://127.0.0.1:8000/docs` mở Swagger. Model được load khi process start; `/readyz` thất bại nếu load không thành công. Request cần location IDs 1–263 và timestamp có timezone. Model/version/release/checksum xem ở `/model-info`; so với labels, annotations và image digest trong Deployment. Không gán `champion` bằng tay để chữa một release verification thất bại.

Cloud MLflow UI: `make m23-port-forward` rồi mở `http://127.0.0.1:5000`. Nếu Compose cũng đang nghe cổng 5000, chọn cổng local khác bằng `kubectl -n mlops port-forward service/mlflow 15000:5000` để chắc chắn đọc cloud Registry. Alias check cần tunnel cloud: `make release-promotion-verify M32_CLOUD_MLFLOW_URI=http://127.0.0.1:15000`.

## GitOps và release

Release model đã approved: `make release-test` rồi `make release-run M31_MODEL_NAME=<name> M31_MODEL_VERSION=<explicit-version>`. Xem `gh run list --workflow m32-release.yml --limit 5` và run summary; phải có Registry version/run ID, artifact checksum, ECR digest, một GitOps commit, Argo CD revision, Pod imageID, prediction và champion read-back. `make release-gitops-verify` chỉ đọc cluster. Direct push bị branch protection từ chối sẽ để image trong ECR nhưng không promote; giải quyết quy trình merge rồi dispatch lại, không force push.

Nếu workflow bị gián đoạn, kiểm tra EKS `publicAccessCidrs` đã bằng danh sách ban đầu trong GitHub run summary. CIDR recovery script giữ snapshot ở runner và bước cleanup `always()`; runner bị hủy đột ngột vẫn có thể cần khôi phục thủ công. Giữ public/private endpoints và tất cả admin CIDRs. Không dùng `kubectl apply` trực tiếp cho serving do Argo CD quản lý.

Argo CD UI: `make argocd-port-forward` rồi mở `https://127.0.0.1:8080`. Password/SSO tuân theo cấu hình cluster, không nằm trong repository. Application `inference-api` phải Synced/Healthy và chỉ sở hữu Deployment, Service, HPA trong `mlops`.

## Monitoring và load test

Terminal riêng:

```bash
kubectl -n monitoring port-forward service/m33-prometheus-server 9090:80
kubectl -n monitoring port-forward service/m33-grafana 3000:80
```

Prometheus tại `http://127.0.0.1:9090/targets`; target `inference-api` của mỗi Running Pod phải UP. Grafana tại `http://127.0.0.1:3000/d/m33-inference/inference-api`, username `admin`. Lấy password qua Kubernetes Secret khi được phép: `kubectl -n monitoring get secret m33-grafana-admin -o jsonpath='{.data.admin-password}' | base64 --decode`. Dashboard có request rate, P95 latency, error rate và Pod scrape status. Status panel là Prometheus `up`, không thay thế Kubernetes readiness.

```bash
kubectl -n mlops get hpa inference-api -w
kubectl -n mlops top pods -l app=inference-api
make monitoring-load-test M33_VUS=10 M33_RATE=20 M33_DURATION_SECONDS=300
```

Chỉ tăng tải trong giới hạn `M33_VUS<=50`, `M33_RATE<=200`, `M33_DURATION_SECONDS<=900` khi system/inference nodes ổn định. HPA target 60% của request 250m CPU/Pod, min1/max3. Quan sát `kubectl -n mlops describe hpa inference-api`, Deployment replicas và `kubectl get nodeclaims`. Sau tải, chờ stabilization 300 giây cộng HPA sync để xác nhận scale-in. Bài test dev đã chứng minh 1→3→1 Pods, nhưng traffic tập trung vào Pod cũ nên chưa chứng minh tăng thông lượng.

## Backup, khôi phục và dọn dẹp

```bash
make cloud-backup
make cloud-restore-check BACKUP_KEY=postgres/<timestamp>.dump
```

Cloud backup là PostgreSQL dump + metadata/checksum ở S3; model artifacts lưu ở bucket S3 khác. Restore check chỉ thử vào database tạm. Primary restore khi tạo PVC mới phải dùng `RESTORE_KEY` với `make cloud-deploy`; script từ chối ghi lên database đang chạy hoặc tạo DB rỗng khi S3 đã có backup. Local Compose có `make mlflow-backup` và `make mlflow-restore-check BACKUP_FILE=backups/<file>.dump`; Docker volume artifacts cần backup riêng.

Thứ tự cleanup khi **thực sự muốn dừng môi trường**: xác nhận dữ liệu và image cần giữ; gỡ monitoring bằng `make monitoring-cleanup`; xử lý Application/serving theo GitOps; chạy `make cloud-teardown` (backup/verify rồi **xóa toàn bộ namespace `mlops`, gồm inference và PostgreSQL/PVC**); `make k8s-cleanup` khi không còn workloads; cuối cùng review destroy và chạy `make tf-platform-down`. Target platform down tự gọi cloud teardown/k8s cleanup; không gọi lặp một cách máy móc. Persistent S3/ECR và bootstrap Terraform state không thuộc platform destroy. `make m31-cleanup` là lệnh thủ công cũ, không dùng khi Argo CD đang sở hữu serving. Không chạy `docker compose down -v` nếu cần giữ local MLflow data.

## Lỗi hay gặp

- AWS token/profile sai: `aws sts get-caller-identity`, kiểm tra `AWS_PROFILE` và quyền IAM.
- EKS timeout: kiểm tra kubeconfig, API CIDRs và IP egress hiện tại.
- Pod Pending: `kubectl -n mlops describe pod <pod>`, node labels/taints, PVC/AZ, NodePool/NodeClaim.
- HPA `<unknown>`: Metrics Server add-on, `kubectl top` và CPU requests.
- Grafana Pending khi upgrade: chart dùng `Recreate` vì system node không đủ request cho hai Pods; dashboard ConfigMap được poll 30 giây.
- MLflow 403 qua tunnel: xác nhận đang dùng cloud service và Host header hợp lệ; xem logs MLflow.
