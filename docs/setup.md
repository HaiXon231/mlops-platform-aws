# Thiết lập môi trường và triển khai từ đầu

Hướng dẫn này dùng `environment=dev`, vùng `ap-southeast-1` và Linux. Đây là trình tự từ source; **chưa có bằng chứng toàn bộ trình tự đã chạy trên một AWS account mới**. Mỗi `apply` cần plan được review và mỗi `push`/Job có thể phát sinh phí. Không dùng hướng dẫn này để thay đổi môi trường đang chạy mà chưa xem live state.

## 1. Công cụ, account và cấu hình local

Cài Python 3.12, uv, Git, Make, Docker, AWS CLI v2, Terraform 1.16.6, kubectl, Helm và gh. Clone repository, chạy `make training-setup` và `make check`. `uv.lock` pin Python dependencies; Terraform lockfiles pin providers; chart versions nằm trong scripts/values. Docker daemon phải hoạt động cho image build. Trên Ubuntu, người vận hành cần có quyền Docker và dung lượng cho Parquet/model images.

```bash
git clone https://github.com/HaiXon231/mlops-platform-aws.git
cd mlops-platform-aws
make training-setup
make check
make help
export AWS_PROFILE=mlops-dev  # Đổi thành profile của bạn trên account khác.
export AWS_REGION=ap-southeast-1
aws sts get-caller-identity
```

Profile AWS phải có quyền S3, ECR, IAM, EC2, VPC, EKS, Pod Identity và Terraform backend phù hợp. Kiểm tra quota EKS/EC2/Spot, ngân sách; không đặt AWS keys trong Git hoặc Pod. Terraform variables mặc định `aws_profile=mlops-dev`, `aws_region=ap-southeast-1`, `environment=dev`; Makefile truyền profile hiện tại qua `TF_VAR_aws_profile`. Nếu dùng account khác, chọn profile của chính bạn.

Tạo `.env` từ `.env.example` chỉ khi chạy MLflow Compose local. `MLFLOW_DB_PASSWORD` được nhập qua shell khi tạo cloud Secret, `GRAFANA_ADMIN_PASSWORD` qua shell khi deploy monitoring; không commit chúng. Dữ liệu `dataset/`, `artifacts/`, `backups/` và Terraform states đều Git-ignored; chuẩn bị phương án backup riêng.

## 2. Cấu hình cho AWS account và GitHub repository khác

Persistent S3 bucket names lấy account ID tự động, nhưng **toàn bộ project chưa phải template đa account/đa region**. Trước khi apply/release trên fork, review các chỗ sau và điều chỉnh thành giá trị của bạn:

| Vị trí | Giá trị đang pin | Việc cần làm |
| --- | --- | --- |
| `infra/terraform/platform/m32_release_iam.tf` | GitHub owner/repo và immutable owner/repo IDs | Dùng identity repository thực, kiểm tra GitHub OIDC subject; provider OIDC là data source và phải tồn tại trong account |
| `.github/workflows/m32-release.yml` | Repository IDs, IAM role ARN, region | Khớp trust policy, role ARN từ Terraform output và branch `main` |
| `scripts/m32_release.sh`, `scripts/m32_gitops.py`, `scripts/m31-validate-manifest.py` | EKS name, ECR account/repository URI | Thay bằng cluster/repository thực; chạy unit tests sau sửa |
| `scripts/m32_eks_cidr.py`, `scripts/k8s-common.sh`, một số bootstrap manifests | Cluster/NodeClass `mlops-aws-dev` | Giữ nếu cũng dùng `environment=dev`; nếu đổi tên phải update cùng nhau |
| `deploy/argocd/inference-application-*.yaml` | `repoURL` của tác giả | Trỏ đến repository có thể đọc được bởi Argo CD |
| `deploy/gitops/inference/deployment.yaml` | ECR digest của account gốc | Build/push image của bạn rồi thay bằng digest thật trước sync |
| `infra/terraform/**/versions.tf`, `infra/terraform/platform/network.tf` | Backend region và hai AZ Singapore | Review kỹ nếu đổi region; không đổi một biến đơn lẻ |

Lấy GitHub identity qua `gh api repos/<owner>/<repo> --jq '{id,owner_id:.owner.id}'`. Xem customization của OIDC subject trên GitHub trước khi viết trust; role dùng subject bất biến gồm ID owner/repo và `ref:refs/heads/main`. Account mới cần GitHub OIDC provider `https://token.actions.githubusercontent.com` với audience `sts.amazonaws.com`; Terraform platform hiện **đọc provider đã tồn tại**, không tự tạo provider. Tạo hoặc import provider theo quy trình IAM của account trước plan. Không nới trust sang toàn bộ repositories/branches. Các thay đổi Terraform/trust cho account mới có thể làm thay đổi IAM; review plan, không áp dụng trực tiếp lên dev hiện tại. Workflows chỉ hoạt động khi role, repo identity, branch protection và Kubernetes RBAC thống nhất. Đây là giới hạn tái triển khai còn phải kiểm thử end-to-end trên account mới.

`TF_VAR_eks_public_access_cidrs` phải là JSON list ít nhất một IPv4 `/32` quản trị; không dùng `0.0.0.0/0`. Nếu IP egress đổi, đọc CIDRs live và review plan trước khi cập nhật; không ghi IP cá nhân vào Git. EKS có public và private API endpoints; GitHub release tạm thêm runner `/32` và khôi phục danh sách ban đầu.

## 3. Terraform theo thứ tự dependency

Bootstrap tạo bucket backend bằng **local state**. Persistent tạo S3 datasets/model artifacts/backups và ECR training/inference/MLflow. Platform tạo VPC/EKS/system nodes/Karpenter IAM/Pod Identity/EBS CSI/Metrics Server. Platform và persistent có S3 state key riêng. Đọc `terraform show` nếu plan ghi ra file, kiểm tra không có replace/destroy bất ngờ.

```bash
make tf-bootstrap-init
make tf-bootstrap-plan
make tf-bootstrap-apply

make tf-persistent-init
make tf-persistent-plan
make tf-persistent-apply
make tf-persistent-verify

export TF_VAR_eks_public_access_cidrs="$(printf '["%s/32"]' "$(curl -4fsS https://checkip.amazonaws.com)")"
make tf-platform-init
make tf-platform-plan
make tf-platform-apply
make tf-platform-verify
```

Không commit bootstrap state, tfvars, secrets hoặc plan chứa thông tin nhạy cảm. `tf-persistent-verify` dùng `plan -detailed-exitcode`: 0 là no changes, 2 là có diff cần review.

**Terraform phải đúng 1.16.6** (`required_version = "= 1.16.6"` trong `versions.tf`); nếu máy có nhiều bản, dùng `tfenv` và kiểm tra `terraform version` trong cùng shell sẽ chạy `make` (shell không tương tác có thể lấy PATH khác).

**Account đã có sẵn bootstrap và persistent** (account dùng chung, hoặc state local của bootstrap đã mất): `make tf-bootstrap-apply` lỗi `BucketAlreadyOwnedByYou`, và `make tf-persistent-init`/`tf-platform-init` lỗi `The value cannot be empty` vì tên bucket được đọc từ state local của bootstrap. **Không apply lại bootstrap.** Truyền tên bucket trực tiếp (thay `<account-id>`):

```bash
B=mlops-aws-tfstate-dev-<account-id>-ap-southeast-1
terraform -chdir=infra/terraform/persistent init -backend-config="bucket=$B" -backend-config="profile=$AWS_PROFILE"
terraform -chdir=infra/terraform/persistent plan -input=false   # kỳ vọng: No changes
terraform -chdir=infra/terraform/platform init -backend-config="bucket=$B" -backend-config="profile=$AWS_PROFILE"
```

Sau đó `make tf-platform-plan` và `make tf-platform-apply` như bình thường. Đọc plan: chỉ được thêm/sửa tài nguyên platform, không có `destroy`.

## 4. Kubernetes nền, dataset và training

```bash
aws eks update-kubeconfig --profile "$AWS_PROFILE" --region "$AWS_REGION" --name mlops-aws-dev
make k8s-bootstrap
make k8s-verify
make data-download
make training-test
make cloud-images-build
make cloud-images-push
make cloud-data-sync
read -rsp 'New cloud MLflow DB password: ' MLFLOW_DB_PASSWORD; echo
export MLFLOW_DB_PASSWORD
make cloud-deploy
unset MLFLOW_DB_PASSWORD
make cloud-verify
make cloud-train
VERIFY_TRAINING=1 make cloud-verify
```

`k8s-bootstrap` cài Karpenter/Argo CD và NodePools; training Job trên Spot chỉ chạy khi có input. Nếu account thiếu EC2 Spot service-linked role, IAM admin tạo `AWSServiceRoleForEC2Spot` một lần trước Job. `cloud-data-sync` kiểm tra SHA256 và không tự sửa `source_verified=false`. Train Job dùng pipeline cùng local CLI, log run vào cloud MLflow. `make cloud-backup` và `make cloud-restore-check BACKUP_KEY=postgres/<file>.dump` kiểm chứng metadata backup; model artifacts trong bucket riêng cũng phải được giữ.

**Dựng lại cluster khi S3 và ECR đã có dữ liệu** (sau một lần teardown): dataset, model artifacts, image và bản backup MLflow vẫn còn, nên không cần `data-download`, `cloud-images-*`, `cloud-data-sync` hay `cloud-train`. `make cloud-deploy` từ chối tạo database rỗng khi S3 có backup, nên phải khôi phục:

```bash
aws s3 ls s3://mlops-aws-mlflow-backups-dev-<account-id>-ap-southeast-1/postgres/ | tail -4
read -rsp 'New cloud MLflow DB password (>=16 ký tự): ' MLFLOW_DB_PASSWORD; echo
export MLFLOW_DB_PASSWORD
RESTORE_KEY=postgres/<timestamp>.dump make cloud-deploy
unset MLFLOW_DB_PASSWORD
make cloud-verify
```

Job restore in snapshot registry (`registered_models: 1`, alias `champion` trỏ version 1) và `cloud-verify` phải báo `"registered_model_count": 1` cùng `M2.3 platform verification passed`. Mật khẩu là mật khẩu mới do bạn đặt, không cần trùng bản cũ; nhớ nó vì các lần deploy sau từ chối nếu khác.

## 5. Serving và GitOps lần đầu

Sau khi cloud Registry có approved version, dùng terminal 1 chạy `make m23-port-forward`. Terminal 2 chạy:

```bash
make serving-export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
make serving-build
make serving-push
```

Thay image trong `deploy/gitops/inference/deployment.yaml` bằng ECR URI@sha256 do push in ra. Đảm bảo label version đúng, chạy `make serving-manifest-check` và `kubectl kustomize deploy/gitops/inference`. Push repository của bạn theo quy trình Git riêng trước khi Argo CD đọc nó. Sau đó review và áp dụng Application manual:

```bash
kubectl apply -f deploy/argocd/inference-application-manual.yaml
kubectl -n argocd get application inference-api -o wide
kubectl -n mlops get deployment,service inference-api
make m32-adoption-check
```

Application manual ở trạng thái `OutOfSync/Missing` cho tới khi sync lần đầu; chạy lệnh dưới (hoặc bấm Sync trên Argo CD UI) rồi chờ Pod Ready, Karpenter cấp node inference mất vài phút. Nếu `deployment.yaml` đã pin một digest có trong ECR của account (ví dụ account dùng chung), không cần build lại image.

```bash
kubectl -n argocd patch application inference-api --type merge -p '{"operation":{"sync":{"revision":"HEAD"}}}'
```

Chỉ chuyển sang `kubectl apply -f deploy/argocd/inference-application-automated.yaml` sau khi desired/live khớp và API Ready. Application phải có `ignoreDifferences` cho `/spec/replicas` và `RespectIgnoreDifferences=true` trước khi HPA xuất hiện. Ngừng dùng `m31-deploy`/`kubectl apply` trực tiếp cho serving sau adoption.

Để release tự động, Terraform OIDC role/Access Entry cần được apply và `deploy/bootstrap/m32-github-release-rbac.yaml` cần được áp dụng một lần qua `kubectl apply -f`. Kiểm tra quyền namespace, connectivity và branch protection, rồi chạy `make release-test` và `make release-run M31_MODEL_NAME=nyc-taxi-trip-duration M31_MODEL_VERSION=1`. Workflow runner có quyền tạm mở EKS public CIDR đúng `/32`; sau run kiểm tra CIDRs đã khôi phục. [Phase 3](phases/phase-03.md) giải thích điều kiện promote và verification.

## 6. HPA và monitoring

Metrics Server là Terraform EKS add-on, không cài trùng. HPA nằm trong GitOps Kustomize; xác nhận Argo CD Synced/Healthy và `kubectl -n mlops get hpa inference-api` có CPU current/target. Chart repositories và values:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana-community https://grafana-community.github.io/helm-charts
helm repo update
make monitoring-test
read -rsp 'New Grafana admin password: ' GRAFANA_ADMIN_PASSWORD; echo
export GRAFANA_ADMIN_PASSWORD
make monitoring-deploy
unset GRAFANA_ADMIN_PASSWORD
make monitoring-verify
```

Cả hai Services là ClusterIP, không có Ingress. Password lấy từ Kubernetes Secret `m33-grafana-admin` khi cần; không đưa vào docs/Git. `make monitoring-load-test M33_VUS=30 M33_RATE=100 M33_DURATION_SECONDS=600` tạo Job có giới hạn tài nguyên; chỉ chạy sau khi kiểm tra capacity và HPA metrics.

## 7. Xác minh và dọn dẹp

Dùng `make check`, `make cloud-verify`, `make release-gitops-verify`, `make monitoring-verify`; đọc [operations](operations.md) để xem API/UI, backup/restore và shutdown. Cleanup theo phạm vi: monitoring trước, release/inference theo GitOps, cloud data sau backup, Karpenter/bootstrap cuối cùng, rồi mới `make tf-platform-down`. Không chạy Terraform destroy persistent/bootstrap khi chưa có kế hoạch dữ liệu. Đừng coi phần hướng dẫn này là bằng chứng tái triển khai: kết quả dev đã ghi trong [evidence](evidence/), account mới cần kiểm chứng riêng.

## 8. Canary và rollback (Phase 4)

Làm sau khi Phase 3 (serving, HPA, monitoring) đã chạy. Chi tiết kiểm chứng ở [phase-04.md](phases/phase-04.md) và [evidence](evidence/phase-04.md).

Chuẩn bị mỗi terminal mới: `export AWS_PROFILE=mlops-dev AWS_REGION=ap-southeast-1` (thiếu thì `aws` báo `NoRegion`). `terraform apply` chỉ gọi API của AWS, nhưng `kubectl` và `helm` cần IP của bạn nằm trong CIDR `/32` của EKS. Nếu mạng ra Internet bằng nhiều IP (kiểm tra: chạy `curl -4fsS https://checkip.amazonaws.com` sáu lần liên tiếp mà ra các IP khác nhau), `/32` sẽ liên tục hỏng; dùng một mạng ổn định (ví dụ hotspot điện thoại, kiểm tra lại bằng cùng phép thử). Không dùng `0.0.0.0/0`: Terraform và release workflow đều chặn.

1. **Terraform platform** đã gồm `alb_controller.tf` (IAM và Pod Identity cho AWS Load Balancer Controller) và node group `system` 2 node. Với cluster **tạo mới** từ Terraform hiện tại, `min_size = desired_size = 2` ngay từ đầu nên chỉ cần `make tf-platform-apply`. Chỉ khi **nâng cấp một cluster đã tạo ở Phase 3** (node `system` min/desired 1) mới cần lệnh dưới: module EKS bỏ qua `desired_size` trên node group đang chạy, nên nâng `desired` bằng CLI **trước** khi apply `min_size = 2`, nếu không AWS báo `Minimum capacity 2 can't be greater than desired size 1`:
   ```bash
   aws eks update-nodegroup-config --cluster-name mlops-aws-dev --nodegroup-name system --scaling-config minSize=1,maxSize=2,desiredSize=2
   ```
2. **Add-on:** `make canary-addons-deploy` rồi `make canary-addons-verify` (LB Controller 3.6.2, Argo Rollouts 2.43.9). Phải xong trước khi manifest canary được Argo CD đọc; thiếu CRD `Rollout`/`AnalysisTemplate` hoặc IngressClass `alb` thì sync lỗi.
3. **RBAC cho runner release:** `kubectl apply -f deploy/bootstrap/m32-github-release-rbac.yaml`. Trên cluster mới Role chưa tồn tại cho tới khi áp file này; bản hiện tại thêm quyền đọc `rollouts` và `analysisruns`.
4. **Application:** `kubectl apply -f deploy/argocd/inference-application-automated.yaml` trỏ về `main`. Việc này đặt lại `targetRevision`; nếu template Pod khác bản đang chạy thì Rollouts mở một canary, nên bật tải trước (bước 5).
5. **Tải và quan sát:** `make monitoring-load-test M33_TARGET=alb M33_VUS=20 M33_RATE=40 M33_DURATION_SECONDS=900` (tải theo tốc độ cố định vào ALB internal; gate cần traffic, không có thì `canary-request-rate` inconclusive và Rollout dừng chờ), `make canary-status`, `make canary-split M42_WINDOW=45s`.
6. **Release thật:** nếu thư mục có nhiều remote, `gh repo set-default HaiXon231/mlops-platform-aws` trước; rồi `make release-run M31_MODEL_NAME=<name> M31_MODEL_VERSION=<version>`. EKS public CIDR phải là các `/32`; workflow từ chối `0.0.0.0/0`, và `terraform apply` ghi đè `0.0.0.0/0` đặt tay. Bật tải k6 khi commit `m32: release ... by digest` xuất hiện trên `main`.
7. **Dọn dẹp Phase 4 (thứ tự bắt buộc).** Application `inference-api` không có finalizer, nên xóa nó **không** xóa Ingress hay ALB; Ingress phải bị xóa trong lúc LB Controller còn chạy thì ALB mới được dọn. ALB còn tồn tại giữ ENI và security group nên VPC không xóa được.

   ```bash
   export AWS_PROFILE=mlops-dev AWS_REGION=ap-southeast-1
   kubectl -n argocd delete application inference-api
   kubectl -n mlops delete ingress inference-api
   kubectl -n mlops delete rollout.argoproj.io inference-api
   aws elbv2 describe-load-balancers --query "LoadBalancers[].LoadBalancerName" --output text   # chờ tới khi trống (1-3 phút)
   make monitoring-cleanup
   make canary-addons-cleanup
   export TF_VAR_eks_public_access_cidrs="[\"$(curl -4fsS https://checkip.amazonaws.com)/32\"]"   # destroy cũng cần biến này
   make tf-platform-down
   ```

   `make tf-platform-down` hỏi gõ `destroy mlops-aws-dev`, rồi tự backup và kiểm tra Postgres, xóa namespace `mlops`, gỡ Karpenter/Argo CD (`k8s-cleanup`) và `terraform destroy`; **không chạy `cloud-teardown` hay `k8s-cleanup` riêng trước đó**. Khi Terraform hỏi `yes`, đọc plan: chỉ được hủy VPC, EKS, node group, IAM role và Pod Identity của platform, không có S3 hay ECR. Kiểm tra sau khi xong (cả ba phải trống):

   ```bash
   aws eks list-clusters --output text
   aws ec2 describe-nat-gateways --filter Name=state,Values=available,pending --query "NatGateways[].NatGatewayId" --output text
   aws elbv2 describe-load-balancers --query "LoadBalancers[].LoadBalancerName" --output text
   ```

   S3 (datasets, model-artifacts, backups, state) và ECR được giữ để dựng lại; bản backup Postgres mới nhất nằm ở `postgres/` trong bucket backups.
