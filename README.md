# MLOps Platform on AWS

## Phase 3 — M3.2: GitHub Actions và Argo CD GitOps

M3.2 thêm workflow `workflow_dispatch` cho một MLflow model name/version được chỉ định, Export Job chạy trong EKS, release bundle có checksum trên S3, build/push image bằng GitHub OIDC, GitOps digest commit và Argo CD PostSync verification trước khi gán alias `champion`. Hướng dẫn setup, adoption, release, rollback và kiểm chứng nằm trong [M3.2 runbook](docs/m32-runbook.md). Chưa có tài nguyên M3.2 nào được apply trong quá trình viết mã; M3.1 vẫn chạy như trước.

## Phase 3 — M3.1: FastAPI Model Serving

M3.1 đóng gói **đúng Registry version 1** của `nyc-taxi-trip-duration` vào image. Export đọc tag `validation_status=approved`, các tag gate, run metadata, training manifest, feature schema và MLflow model signature. Bundle có SHA-256 cho từng file và checksum tổng; release chứa model name/version, MLflow run ID, source run ID và Git SHA. `artifacts/releases/` được Git ignore. Export không đổi Registry, không gán alias `champion`.

FastAPI chỉ đọc `/app/release` khi worker khởi động; prediction không cần MLflow hoặc S3. API chuẩn hóa timestamp có timezone sang `America/New_York` rồi gọi lại `training.features.preprocess_request` của M2.1. Năm features và categorical dtypes được giữ nguyên. Model load hoặc checksum thất bại làm `/readyz` trả 503, còn `/healthz` vẫn báo process sống. Một worker Uvicorn tải model một lần. `/metrics` xuất counter request/error, histogram latency và model info; labels chỉ gồm method/path/status trong tập cố định và metadata release, không chứa raw request.

### Export và kiểm thử

Yêu cầu Python 3.12, `uv`, Docker, AWS CLI, `kubectl`; dependencies nằm trong `pyproject.toml` và `uv.lock`. Trên máy Ubuntu đã login profile `mlops-dev`:

```sh
make m23-port-forward  # terminal 1: MLflow cloud ở 127.0.0.1:5000
make m31-model-export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
make m31-test
make m31-image-build
```

Nếu port 5000 đang dùng, chạy `kubectl -n mlops port-forward service/mlflow 5001:5000` và đặt `MLFLOW_TRACKING_URI=http://127.0.0.1:5001`. Có thể đổi `M31_MODEL_NAME`, `M31_MODEL_VERSION`, `M31_RELEASE_DIR` khi export, nhưng image và manifest checkpoint này cố định version 1. Export từ chối thư mục output đã tồn tại để tránh ghi đè release. `make m31-test` gồm test API và integration với Registry SQLite local; test tích hợp dùng M2.1 artifact local nếu có. Với cloud Registry, so sánh thêm `mlflow.lightgbm.load_model("models:/nyc-taxi-trip-duration/1")` với `inference.bundle.validate_release(...)` trên cùng các input.

Docker build dùng `deploy/inference/Dockerfile` và lockfile, nhúng bundle vào `/app/release`. Tag local dạng `mlops-inference:m31-v1-<artifact-checksum-prefix>-<code-hash-prefix>`. Dockerfile xác thực checksum và chạy một prediction trước khi hoàn tất build. Để smoke test container sau khi build:

```sh
docker run --rm -p 8000:8000 mlops-inference:m31-v1-<artifact-checksum-prefix>-<code-hash-prefix>
curl -fsS http://127.0.0.1:8000/readyz
curl -fsS http://127.0.0.1:8000/model-info
curl -fsS -X POST http://127.0.0.1:8000/predict -H 'Content-Type: application/json' \
  -d '{"pickup_location_id":1,"dropoff_location_id":2,"pickup_datetime":"2025-04-07T16:00:00Z"}'
```

`/docs` và `/openapi.json` cho thử API. Response ví dụ:

```json
{"predicted_duration_minutes":14.07,"model_name":"nyc-taxi-trip-duration","model_version":"1"}
```

Input phải có đúng ba trường; location IDs là số nguyên 1–263, timestamp ISO 8601 phải có `Z` hoặc offset. Field thiếu, field lạ, datetime naive, NaN/Infinity hoặc ID sai trả 422. `/metrics` có thể xem qua cùng port-forward.

### Kế hoạch EKS sau review

`deploy/gitops/inference/` là Kustomize path cho Argo CD ở M3.2. Deployment/Service `inference-api` trong namespace `mlops` dùng một replica, RollingUpdate, ClusterIP port 8000, probes và node selector/toleration khớp NodePool `inference-on-demand`. Chưa có Ingress, HPA, Prometheus Server, Grafana hay GitHub Actions. Manifest M3.1 đã pin digest ECR của version 1; khi phát hành image khác, phải lấy digest mới từ ECR và thay vào manifest trước khi deploy. `m31-deploy` từ chối digest placeholder toàn số 0.

Sau khi review và cho phép triển khai, người vận hành có thể chạy `make m31-image-push` để push tag immutable vào ECR inference repository hiện có. Nếu source đã đổi sau lúc build, đặt `M31_IMAGE_TAG=<tag đã build và kiểm thử>` trước lệnh push; script xác nhận tag vẫn khớp checksum bundle. Lệnh in ra `repository@sha256:<digest>`; thay image trong `deploy/gitops/inference/deployment.yaml` bằng giá trị đó và kiểm tra label model version 1. Kiểm tra `kubectl get nodepool inference-on-demand` truy cập được EKS; nếu timeout, kiểm tra kết nối tới endpoint public/private và đối chiếu IP public với `publicAccessCidrs`. Chạy `python3 scripts/m31-validate-manifest.py`, rồi mới chạy `make m31-deploy`. `make m31-verify` kiểm tra rollout và resources. `make m31-port-forward` mở Service tại `http://127.0.0.1:8000` để thử `/readyz`, `/predict`, `/metrics`. `make m31-cleanup` chỉ xóa Deployment và Service `inference-api`; PostgreSQL, MLflow, PVC và workload Phase 2 không bị đụng tới.

### Troubleshooting và chi phí

- `/readyz` 503: xem log container để xác định thiếu bundle, checksum sai, schema sai hoặc model không load được; `/healthz` chỉ xác nhận process sống.
- Pod Pending: kiểm tra NodePool `inference-on-demand`, taint `mlops.aws/workload=inference:NoSchedule`, capacity On-Demand và giới hạn 4 vCPU. Karpenter có thể cần tạo EC2 node mới.
- ImagePullBackOff: xác nhận digest ECR, repository/account/region và node IAM pull permission. Placeholder toàn số 0 cố ý bị chặn trước deploy.
- Không export được qua port-forward: kiểm tra MLflow Deployment/Service, phiên AWS, tracking URI và tag approval của version được chỉ định.
- Phát sinh sau deploy: một node On-Demand cho inference nếu pool đang trống, EBS node, ECR image storage, NAT/data transfer và request liên quan. Chưa có chi phí serving EC2 từ checkpoint này khi chưa deploy. Giới hạn CPU/memory và replica cần đo bằng traffic thật trước khi tăng.

## Checkpoint 1.2 — Bootstrap và persistent

Checkpoint 1.2 định nghĩa hạ tầng lưu trữ bền vững theo [kế hoạch](../MLOps_AWS_Implementation_Plan_v1.0.1.md): một S3 bucket cho Terraform state, ba S3 buckets cho datasets/model artifacts/MLflow backups và hai ECR repositories.

Yêu cầu: Terraform 1.16.6, AWS CLI và AWS profile `mlops-dev` có quyền S3, ECR, STS trong `ap-southeast-1`. Credentials được lấy từ profile; không lưu trong repository. Trước khi chạy, đăng nhập lại profile nếu phiên đã hết hạn.

## Khởi tạo

```sh
make tf-bootstrap-init
make tf-bootstrap-plan
make tf-bootstrap-apply
make tf-persistent-init
make tf-persistent-plan
make tf-persistent-apply
make tf-persistent-verify
```

Các target `apply` cần người vận hành chạy chủ động sau khi xem `plan`. `tf-persistent-verify` chạy `validate` và `plan -detailed-exitcode`; exit code 0 nghĩa là không có thay đổi, 2 nghĩa là có drift/thay đổi dự kiến.

Bootstrap giữ `terraform.tfstate` cục bộ tại `infra/terraform/bootstrap/`; hãy sao lưu file này ở nơi an toàn, không commit vào Git và không chuyển bootstrap state sang remote backend ở checkpoint này. Persistent dùng S3 backend được cấu hình từ output `state_bucket_name`, key `persistent/terraform.tfstate`, SSE-S3 và S3 lockfile. Phase platform sau này dùng key riêng `platform/terraform.tfstate` trên cùng bucket.

Bucket state và ba bucket dữ liệu có versioning, SSE-S3, chặn public access và chặn xóa bằng Terraform. Hai ECR repositories có immutable tags, basic scan on push và chặn xóa. S3/ECR vẫn có phí theo dung lượng, request và truyền dữ liệu khi sử dụng.

## Checkpoint 1.3 — VPC và EKS

Platform có state riêng tại `platform/terraform.tfstate` trong bucket bootstrap. Terraform pin VPC module `6.7.3` và EKS module `21.26.0`. VPC `10.20.0.0/16` có hai public và hai private subnets tại `ap-southeast-1a/b`. Một NAT Gateway ở AZ đầu cho lưu lượng ra Internet từ private subnets; S3 Gateway Endpoint đưa lưu lượng S3 từ private subnets ra khỏi đường NAT. EKS `mlops-aws-dev` chạy Kubernetes `1.36` với một Managed Node Group `system` trong private subnets (`t3.large`, On-Demand, AL2023, EBS gp3 30 GiB, min/desired/max = 1/1/2). Không có node IP công khai hoặc SSH.

Trước khi plan/apply, phải cung cấp **IP công khai hiện tại của máy quản trị** dưới dạng `/32`; cấu hình không chấp nhận dải rộng hay mặc định `0.0.0.0/0`. Không commit IP cá nhân vào repository:

```sh
export TF_VAR_eks_public_access_cidrs='["<YOUR_PUBLIC_IPV4>/32"]'
make tf-platform-init
make tf-platform-plan
# Chỉ sau khi review plan:
make tf-platform-apply
make tf-platform-verify
```

`tf-platform-verify` cần AWS CLI và `kubectl`; target đọc trạng thái cluster/node group, đợi node Ready và liệt kê system pods. Nếu `mlops-dev` hết phiên đăng nhập hoặc không đủ quyền IAM/EKS/EC2, hãy xử lý quyền trước khi apply. EKS Access Entry cấp cluster admin cho IAM principal thực sự đứng sau profile `mlops-dev`; xem output `eks_cluster_admin_principal_arn` trước khi apply.

Để tháo dỡ **chỉ platform**, sao lưu dữ liệu ứng dụng cần giữ rồi chạy `make tf-platform-down`. Target yêu cầu nhập `destroy mlops-aws-dev` và Terraform còn yêu cầu xác nhận destroy. Bootstrap và persistent dùng state riêng nên không bị target này quản lý.

**Chi phí khi apply:** EKS control plane, NAT Gateway/EIP, EC2 `t3.large`, EBS gp3, CloudWatch control-plane logs và lưu lượng truyền dữ liệu có thể phát sinh phí; một NAT cho hai AZ còn có rủi ro gián đoạn và phí truyền dữ liệu giữa AZ. S3 Gateway Endpoint không có phí theo giờ. Dừng platform bằng `tf-platform-down` sau khi demo để hạn chế chi phí.

## Checkpoint 1.4 — Karpenter và Argo CD

Terraform platform bổ sung submodule [Karpenter của EKS module v21.26.0](https://github.com/terraform-aws-modules/terraform-aws-eks/tree/v21.26.0/modules/karpenter), tạo IAM controller/node role và instance profile, EKS Pod Identity association, node access entry, SQS queue và EventBridge rules cho Spot interruption. Nó không quản lý Helm hoặc Kubernetes objects. Karpenter chart và CRD chart được pin `1.14.1` (bản LTS; [mã nguồn xác nhận hỗ trợ Kubernetes đến 1.36](https://github.com/aws/karpenter-provider-aws/blob/v1.14.1/pkg/providers/version/version.go)). [Argo CD chart `10.10.5`](https://github.com/argoproj/argo-helm/blob/argo-cd-10.10.5/charts/argo-cd/Chart.yaml) cài Argo CD `v3.5.5`; chart khai báo Kubernetes `>=1.25`. Cả hai chạy non-HA trên node `system`; Argo CD chỉ có ClusterIP.

Sau khi review Terraform plan, người vận hành tự chạy `make tf-platform-apply`, cấu hình `kubectl` cho `mlops-aws-dev`, rồi:

```sh
aws eks update-kubeconfig --profile mlops-dev --region ap-southeast-1 --name mlops-aws-dev
make k8s-bootstrap
make k8s-verify
make k8s-smoke
make argocd-port-forward  # Mở https://localhost:8080 trên máy cá nhân
```

`k8s-bootstrap` cài Karpenter CRDs/controller bằng Helm, áp EC2NodeClass và hai NodePools, rồi cài Argo CD. `training-spot` và `inference-on-demand` có label/taint riêng; mỗi pool giới hạn 4 vCPU, chỉ cho phép các instance CPU x86_64 đã liệt kê. Không có node mới nếu thiếu Pod phù hợp. `k8s-smoke` tạo một Job nhỏ trên Spot, chờ NodeClaim được tạo, xóa Job rồi chờ NodeClaim được thu hồi. **Job này có thể làm phát sinh phí EC2/EBS và NAT data transfer.** Nếu Spot thiếu capacity trong các instance types đã giới hạn, smoke test sẽ báo lỗi; không tự tăng ngân sách.

Trước lần dùng Spot đầu tiên, tài khoản AWS phải có service-linked role `AWSServiceRoleForEC2Spot`. `k8s-smoke` kiểm tra role này trước khi tạo Job. Nếu role chưa có, IAM administrator có quyền `iam:CreateServiceLinkedRole` chạy một lần `aws iam create-service-linked-role --aws-service-name spot.amazonaws.com --profile mlops-dev`. Đây là role dùng chung toàn tài khoản, không thuộc Terraform platform và không bị `tf-platform-down` xóa. [AWS hướng dẫn role này cho Spot API/CLI](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/service-linked-roles-spot-instance-requests.html).

Để thử GitOps sau khi repository có URL Git truy cập được bởi Argo CD và đã push thư mục `deploy/gitops/smoke`:

```sh
export GITOPS_REPO_URL='https://github.com/<owner>/<repo>.git'
export GITOPS_REVISION='<branch-or-commit>'
make gitops-smoke
```

Application mẫu tự đồng bộ một ConfigMap vô hại và tự xóa resource khi Application bị xóa. Repository private cần cấu hình quyền truy cập Argo CD **bên ngoài repository này**; không ghi token/SSH key vào Git. Mẫu không được kích hoạt tự động trong bootstrap. `deploy/gitops/workload-application.yaml.tpl` là khung Application đồng bộ thủ công cho các phase sau; thay các biến bằng repo/path/namespace thật rồi review trước khi áp dụng. Workload training/inference cần selector `mlops.aws/workload: training|inference` và toleration tương ứng.

Cleanup theo thứ tự: backup và xóa workload ứng dụng, chạy `make k8s-cleanup` để xóa Job/Application mẫu, NodePools và đợi mọi NodeClaim/EC2 node Karpenter biến mất, sau đó gỡ Helm releases. `make tf-platform-down` yêu cầu xác nhận, chạy cleanup trước rồi mới destroy **platform state**; persistent state không bị đụng tới. Cleanup sẽ dừng nếu còn Application khác hoặc NodeClaim chưa thu hồi. SQS/EventBridge tính phí theo request/event; Karpenter controller/Argo CD dùng tài nguyên trên node `system`, còn các node do NodePools tạo sẽ tính phí EC2/EBS trong lúc tồn tại.

## Phase 2 — M2.1: Chuẩn bị dữ liệu và train local

Nguồn là [NYC TLC Yellow Taxi Trip Records dạng Parquet](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page). Cấu hình trong `configs/training.yaml` dùng 2025-01/02 cho **train**, 2025-03 cho **validation**, 2025-04 cho **test**; các tháng sau không tham gia để dành kiểm tra drift. Cần [uv](https://docs.astral.sh/uv/getting-started/installation/); `pyproject.toml`, `uv.lock` và `.python-version` cố định môi trường Python 3.12. Chạy tại thư mục gốc repository:

```sh
make training-setup
make training-test
make data-download    # Dùng cache nếu bốn file Parquet đã có; nếu thiếu mới tải.
make train-local
```

`data-download` kiểm tra Parquet, SHA256 và ghi sidecar metadata trong `dataset/`. File đã tồn tại nhưng không có metadata tải xuống sẽ được đánh dấu `source_verified=false`, vì không thể tự khẳng định nguồn gốc của file cũ. `train-local` **không tự tải dữ liệu**. Có thể dùng CLI để thay config hoặc đường dẫn mà không sửa code:

```sh
uv run --locked mlops-train train --config configs/training.yaml \
  --input-dir /path/to/parquet --output-dir /path/to/runs
uv run --locked mlops-train predict --model-dir /path/to/runs/<RUN_ID> \
  --request '{"tpep_pickup_datetime":"2025-04-07T21:00:00","PULocationID":1,"DOLocationID":2}'
```

Pipeline chỉ đọc bốn cột cần thiết theo Parquet batches. Nó bỏ pickup nằm ngoài tháng của file, timestamp/location ID thiếu hoặc sai, và duration ngoài 1–180 phút. Sau làm sạch, mỗi tháng được lấy mẫu ngẫu nhiên trên **toàn bộ dòng hợp lệ**, tối đa 100.000, với seed cố định. Features theo đúng thứ tự: `PULocationID`, `DOLocationID` (categorical taxi zones 1–263), `pickup_hour`, `pickup_day_of_week` (thứ Hai = 0), `pickup_month`. Pickup dùng giờ địa phương NYC không kèm timezone; dropoff chỉ tạo nhãn duration, không đi vào features. Serving sẽ dùng lại `training.features.preprocess_request` và `training.model.load_model`.

Mỗi run nằm trong `artifacts/training/<RUN_ID>/`: `model.txt`, `metrics.json` (MAE/RMSE phút trên validation/test và baseline trung vị train), `feature_schema.json` (hợp đồng input/feature), `manifest.json` (SHA256, nguồn, số dòng trước/sau lọc và sampling, config, seed, Git SHA, phiên bản dependencies). Validation dùng cho early stopping; test chỉ đánh giá cuối. `dataset/`, `artifacts/` và `.venv/` được Git ignore. Các artifact/metrics này có thể được import vào MLflow mà không cần train lại hoặc thay đổi hợp đồng feature.

## Phase 2 — M2.2: MLflow Tracking và Model Registry local

`compose.yaml` chạy PostgreSQL 17.11 và MLflow 3.6.0. PostgreSQL giữ metadata, volume `mlflow_artifacts` giữ model/artifacts; cả hai volume tồn tại qua `make mlflow-down` rồi `make mlflow-up`. MLflow phục vụ artifacts qua Tracking Server, nên client chỉ cần HTTP URI, không cần biết đường dẫn trong container ([MLflow artifact proxy](https://www.mlflow.org/docs/latest/self-hosting/architecture/tracking-server/)). UI chỉ mở tại `http://127.0.0.1:5000`.

```sh
cp .env.example .env                 # Đổi MLFLOW_DB_PASSWORD thành mật khẩu riêng; không commit .env
make training-setup
make mlflow-up
make mlflow-verify
make mlflow-validate RUN_DIR=artifacts/training/<RUN_ID>    # Xem gate, không đăng ký
make mlflow-import-run RUN_DIR=artifacts/training/<RUN_ID>  # Import run M2.1 đã có, không train lại
make mlflow-train                  # Train run mới rồi tracking/gate
make mlflow-test
make mlflow-backup                 # Ghi backups/mlflow-<UTC>.dump
make mlflow-restore-check BACKUP_FILE=backups/<FILE>.dump
make mlflow-down                   # Giữ nguyên volumes; không dùng down -v
```

`make train-local` vẫn chạy hoàn toàn không cần MLflow. `mlflow-train` và `mlflow-import-run` dùng `configs/mlflow.yaml`: model đăng ký tên `nyc-taxi-trip-duration`, ngưỡng cải thiện validation MAE mặc định 1%. Mọi run được import đều có parameters, MAE/RMSE, manifest, feature schema và LightGBM model với signature. Run bị gate từ chối vẫn hiện trong experiment nhưng không có model version. Nếu chưa có `champion`, gate so với candidate đã duyệt tốt nhất trên cùng validation sample; nếu chưa có candidate thì so với median baseline. Khác validation dataset/metric sẽ báo lỗi. **Không tự gán alias `champion`**; Phase 3 mới promote sau khi deployment đạt yêu cầu. Test MAE chỉ dùng báo cáo cuối, không tham gia gate.

`source_verified` trong manifest được giữ nguyên khi import: file cache cũ chưa xác minh nguồn vẫn là `false`. Import lại cùng run trả về MLflow run/model version đã có. Sau khi restart Compose, dùng `make mlflow-verify`, kiểm tra run/model trong UI và artifacts của run. Backup là bản dump PostgreSQL nhất quán; lệnh restore check chỉ tạo database kiểm thử tạm thời trong container rồi xóa database kiểm thử đó, không ghi vào database chính. Backup database **chưa chứa** dữ liệu volume artifact: cần giữ/backup volume artifact riêng để khôi phục đầy đủ Registry.

Để dự đoán từ một version, tải bằng `mlflow.lightgbm.load_model("models:/nyc-taxi-trip-duration/<VERSION>")`, rồi truyền DataFrame do `training.features.preprocess_request(...)` tạo. Hai location ID phải giữ kiểu Pandas `category` như lúc train; gọi generic `mlflow.pyfunc.predict` trực tiếp với JSON/integers sẽ mất kiểu category và chưa được hỗ trợ ở checkpoint này. FastAPI ở phase sau cần tái sử dụng hàm preprocessing này.

## Phase 2 — M2.3: Training trên EKS và Registry trên S3

Terraform **persistent** thêm một ECR repository `mlops-aws-mlflow-dev`; training dùng repository sẵn có. Terraform **platform** thêm EBS CSI add-on và bốn Pod Identity roles: EBS CSI, MLflow, training, backup. EBS CSI dùng policy AWS `AmazonEBSCSIDriverEKSClusterScopedPolicy`; ba role ứng dụng chỉ truy cập prefix S3 của mình. Add-on version được EKS chọn theo bản mặc định tương thích Kubernetes 1.36 tại thời điểm `plan`; xem output `ebs_csi_addon_version` và chốt lại trước khi apply. [AWS hướng dẫn kiểm tra phiên bản và IAM của add-on](https://docs.aws.amazon.com/eks/latest/userguide/retreive-iam-info.html).

Kubernetes manifests trong `deploy/platform/mlops/` tạo `mlops` namespace, `mlops-gp3` StorageClass (`ebs.csi.aws.com`, `WaitForFirstConsumer`), PostgreSQL StatefulSet 1 replica với PVC 10 GiB, MLflow Deployment/ClusterIP và ba service accounts. PostgreSQL và MLflow chỉ chạy trên managed node group `system`. MLflow giữ metadata trong PostgreSQL và proxy artifacts trực tiếp lên `s3://<model-artifacts-bucket>/mlflow/`; local Compose M2.2 vẫn dùng database và volume riêng. UI trên EKS chỉ truy cập qua port-forward. Mật khẩu database nằm trong Kubernetes Secret `mlflow-db`, được nhập qua environment của shell, không có trong Git.

Training Job dùng image Python 3.12 và dependencies từ `uv.lock`, tải đúng bốn tháng Parquet cùng metadata từ S3 Datasets, kiểm tra SHA256, rồi chạy nguyên pipeline M2.1 và gate M2.2. Job có selector/toleration cho `training-spot`, giới hạn 2 CPU/3 GiB, timeout 2 giờ, retry tối đa hai lần và tự dọn Job sau một ngày. Bốn input vẫn là 2025-01/02 train, 2025-03 validation, 2025-04 test; không đổi seed, sampling, features hay MAE gate. Các file local trong Pod là `emptyDir`; run/model artifacts được MLflow ghi vào S3 trước khi Job được xem là hoàn thành.

### Trình tự triển khai (người vận hành chạy sau khi review)

```sh
aws login --profile mlops-dev
export TF_VAR_eks_public_access_cidrs='["<CURRENT_ADMIN_IP>/32"]'
make tf-persistent-init && make tf-persistent-plan  # chỉ thêm mlflow ECR
make tf-persistent-apply
make tf-platform-init && make tf-platform-plan       # kiểm tra không replace VPC/EKS/system
make tf-platform-apply
make m23-images-build
make m23-test
make m23-images-push
make m23-data-sync                              # cần đủ 4 Parquet trong dataset/
export MLFLOW_DB_PASSWORD='<strong-random-password>'
make m23-deploy
make m23-verify
make m23-train
VERIFY_TRAINING=1 make m23-verify
make m23-port-forward                          # http://127.0.0.1:5000
```

Các script lấy bucket/repository URL từ Terraform persistent outputs; `m23-images-build` chỉ build local, `m23-images-push` mới đăng nhập ECR và push. Có thể đặt `IMAGE_TAG` để chọn tag immutable; mặc định tag là hash của source/config/build files. `m23-data-sync` từ chối ghi đè object S3 có SHA256 khác và giữ `source_verified` đúng giá trị trong sidecar. Nếu `source_verified=false`, tải lại từ nguồn chính thức bằng `make data-download` và kiểm tra metadata trước khi sync; script không tự sửa thành `true`. **Không** đặt AWS access keys vào Pod hoặc Git: boto3 lấy credentials ngắn hạn qua Pod Identity. Role training chỉ đọc S3, role MLflow chỉ đọc/ghi prefix artifacts, role backup chỉ đọc/ghi prefix dump; không có quyền xóa S3 objects.

### Backup, thử restore và recovery

```sh
make m23-backup                              # In BACKUP_KEY=postgres/<timestamp>.dump
make m23-restore-check BACKUP_KEY=postgres/<timestamp>.dump
```

Backup Job chạy `pg_dump -Fc` nhất quán, thử restore dump vào database tạm, ghi dump cùng JSON metadata/SHA256 lên S3 MLflow Backups và kiểm tra `HEAD` của cả hai objects. `restore-check` tải dump từ S3, kiểm SHA256, thử restore vào database tạm rồi so sánh số experiments/runs/model versions, run IDs, model version/run ID và aliases. Nó không ghi vào database chính.

Khi tạo lại EKS/PVC mới, truyền `RESTORE_KEY=postgres/<timestamp>.dump` cho `make m23-deploy`. Script thử restore trước, chỉ restore vào database chính nếu **trống** và MLflow Deployment chưa chạy, rồi mới khởi động MLflow. Nếu bucket đã có backup mà PVC chưa có, deploy từ chối khởi tạo database mới khi thiếu `RESTORE_KEY`. Sau restore, chạy `make m23-verify`, mở UI và thử load một Registry version/artifact. M2.2 local Registry **không** được tự đồng bộ sang cloud Registry.

### Teardown và chi phí

`make m23-teardown` luôn backup và thử restore trước khi xóa PostgreSQL/PVC. Backup hoặc kiểm chứng thất bại sẽ dừng teardown. Sau đó script xóa training Jobs, MLflow/PostgreSQL, đợi EBS PV được CSI xóa, rồi xóa namespace/StorageClass. `make tf-platform-down` gọi bước này trước `k8s-cleanup` và chỉ destroy **platform state** sau xác nhận tương tác; persistent S3/ECR và Terraform state giữ nguyên. Không dùng `destroy -auto-approve` hay `docker compose down -v`.

Chi phí phát sinh khi chạy: EKS control plane, `system` EC2, NAT/EIP sẵn có, EBS gp3 10 GiB cho PostgreSQL, Spot EC2/EBS trong lúc training, S3 storage/requests, ECR storage, CloudWatch logs và data transfer. Không tạo RDS, EFS, ALB hay interface endpoint. Mẫu này dùng một PostgreSQL replica, một AZ cho mỗi EBS volume và một NAT Gateway nên chưa có HA. Nếu system node được thay ở AZ khác AZ của PVC, PostgreSQL có thể Pending; cần đưa system node trở lại AZ chứa volume hoặc restore từ S3 vào PVC mới. Nếu Pod Pending, kiểm tra `kubectl -n mlops describe pod <pod>` (PVC/CSI, thiếu tài nguyên hoặc Pod Identity); nếu Job thất bại, kiểm tra `kubectl -n mlops logs job/<job>` và Karpenter NodeClaims. Để xem tài nguyên node system, chạy `make m23-verify`; các requests khai báo của Argo CD, Karpenter, PostgreSQL và MLflow tổng khoảng **850m CPU/1856 MiB**, chưa gồm CSI/CoreDNS và DaemonSets, nên phải kiểm tra allocatable thực tế trên EKS trước khi chấp nhận vận hành.
