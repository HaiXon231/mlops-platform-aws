# M3.2 — CI/CD, GitOps và champion promotion

## Luồng phát hành

1. `workflow_dispatch` nhận `model_name` và **version số cụ thể**; không chọn latest. Workflow kiểm tra immutable GitHub repository ID/owner ID, chạy tests, dùng OIDC nhận AWS credentials và build control image. Runner không cần truy cập MLflow hoặc Kubernetes.
2. Workflow ghi Export Job vào `deploy/gitops/export/` và commit lên `main` bằng non-force push. Argo CD Application `inference-release-export` chạy Job trong namespace `mlops` bằng Pod Identity `inference-release-export`. Job đọc Registry qua Service nội bộ, gọi lại M3.1 exporter, kiểm tra approval/provenance/signature, rồi upload bundle vào `s3://mlops-aws-model-artifacts-dev-160616607747-ap-southeast-1/releases/m32/<name>/v<version>/<release-id>/`. `complete.json` được ghi cuối cùng và là dấu hiệu bundle hoàn chỉnh.
3. Workflow đợi `complete.json`, tải mọi file, kiểm tra SHA-256 và `validate_release`, chạy tests, build image FastAPI từ bundle, push vào ECR inference repository hiện có và lấy digest. `training_git_sha` lấy từ model Registry; `serving_git_sha` là source commit của workflow và được bake vào image. Hai SHA không bị đồng nhất một cách ngầm định.
4. Workflow commit digest, version, release ID và PostSync Job vào `deploy/gitops/inference/`. Argo CD sync Deployment/Service. PostSync Job đối chiếu desired image với Deployment và imageID thực tế của Pod, readiness, `/model-info`, `/predict`, release checksum và Registry approval. Job dùng Lease `inference-champion` để tuần tự hóa promotion; sau kiểm chứng mới gọi MLflow `set_registered_model_alias(..., "champion", version)`. Promotion cùng version là no-op. `verified.json` hoặc `failed.json` trên S3 lưu kết quả cho workflow.

Chuỗi evidence: Registry version/run → S3 `complete.json` và bundle → ECR digest → GitOps commit → Argo CD sync/Job log → S3 verification status → MLflow alias. Bundle và image artifacts không vào Git. PostSync Job chỉ thuộc Application inference; MLflow/PostgreSQL/PVC không thuộc phạm vi sync/prune của Application này.

## IAM/OIDC

Terraform tại `infra/terraform/platform/m32_release_iam.tf` **đọc** GitHub OIDC provider đã tồn tại `token.actions.githubusercontent.com`. Repo hiện có owner `HaiXon231` (ID `204581707`), repo `mlops-platform-aws` (ID `1424395869`), default branch `main`; GitHub API hiện trả `use_immutable_subject=true`. Trust policy dùng `aud=sts.amazonaws.com` và subject chính xác `repo:HaiXon231@204581707/mlops-platform-aws@1424395869:ref:refs/heads/main`. Workflow kiểm tra các ID trước khi xin credentials. Nếu repo bị chuyển chủ, đổi tên, branch hoặc subject format, phải kiểm tra lại API và cập nhật policy trước khi release.

- GitHub role: chỉ đọc `releases/m32/*` trong model artifacts S3 bucket; ECR login và push/describe vào `mlops-aws-inference-dev`.
- Export Pod Identity: đọc `mlflow/*`, đọc/ghi `releases/m32/*` trong cùng bucket.
- Verify Pod Identity: chỉ ghi `verified.json`/`failed.json` trong release prefix. Kubernetes Role của verifier chỉ đọc Deployment/Pods và tạo/cập nhật Lease trong `mlops`.
- Workflow có `id-token: write`, `contents: write`, `pull-requests: write`; không có AWS access keys lưu trong GitHub. Actions được pin SHA.

## Kiểm tra trước khi apply

Từ repository root, sau khi đăng nhập AWS:

```bash
export AWS_PROFILE=mlops-dev AWS_REGION=ap-southeast-1
make m32-test
terraform -chdir=infra/terraform/platform fmt -check
terraform -chdir=infra/terraform/platform validate
export TF_VAR_eks_public_access_cidrs='["<CURRENT_ADMIN_PUBLIC_IP>/32"]'
make tf-platform-plan
make m32-adoption-check
kubectl kustomize deploy/gitops/export
kubectl kustomize deploy/gitops/inference
gh api repos/HaiXon231/mlops-platform-aws/actions/oidc/customization/sub
```

Plan được kiểm chứng khi implementation: **8 additions, 0 changes, 0 destroys** với CIDR hiện tại `14.187.41.169/32`. Kiểm tra lại CIDR và plan ngay trước lúc apply. Workspace hiện có nhiều file M2/M3.1 chưa commit; GitHub remote vẫn ở commit đầu `56700c4`. Cần review, commit và push toàn bộ baseline liên quan trước khi Argo CD theo dõi `main` hoặc chạy workflow. Push này là bước người vận hành thực hiện sau review.

## Thứ tự triển khai sau khi được duyệt

Các lệnh dưới đây **chưa được chạy** khi implementation M3.2. Giữ nguyên Deployment M3.1 trong quá trình adoption.

1. Review Git diff và Terraform plan; đưa source, workflow, Terraform, manifests M2/M3.1/M3.2 lên `main` bằng quy trình Git của repo. Kiểm tra `git status` và `git ls-remote --heads origin` để chắc GitHub có đúng digest M3.1 đang chạy.
2. Apply riêng Terraform platform sau review, kiểm tra plan chỉ thêm 3 IAM roles, 3 inline policies và 2 EKS Pod Identity associations:

   ```bash
   make tf-platform-plan
   make tf-platform-apply
   terraform -chdir=infra/terraform/platform output m32_github_release_role_arn
   ```

3. Kiểm tra live/desired trước khi Argo quản lý. Lệnh `make m32-adoption-check` phải thành công. Tạo **manual** serving Application, xem diff trong Argo CD; chỉ sync nếu Deployment/Service cùng digest, probe, resource và scheduling. Có thể có ServiceAccount/Role/RoleBinding verifier mới. Không đổi image trong bước adoption.

   ```bash
   make m32-adoption-check
   kubectl apply -f deploy/argocd/inference-application-manual.yaml
   kubectl -n argocd get application/inference-api -o yaml
   # Trong Argo CD UI: xem Diff rồi bấm Sync cho toàn bộ Application; không dùng selective sync.
   kubectl -n mlops rollout status deployment/inference-api --timeout=10m
   make m32-adoption-check
   ```

   Sau khi app `Synced/Healthy` và API không gián đoạn, bật automated sync **không prune** và cài exporter Application. Từ đây ngừng dùng `make m31-deploy`/`kubectl apply -k deploy/gitops/inference` cho serving workload; chỉ sửa GitOps manifest qua Git.

   ```bash
   kubectl apply -f deploy/argocd/inference-application-automated.yaml
   kubectl apply -f deploy/argocd/inference-export-application.yaml
   make m32-gitops-verify
   ```

4. Dispatch release cùng approved v1 để kiểm chứng digest update mà không train lại:

   ```bash
   gh workflow run m32-release.yml --ref main -f model_name=nyc-taxi-trip-duration -f model_version=1
   gh run list --workflow m32-release.yml --limit 5
   gh run watch 123456789  # thay bằng run ID từ gh run list
   make m32-gitops-verify
   make m23-port-forward  # terminal riêng, giữ mở
   make m32-promotion-verify MLFLOW_TRACKING_URI=http://127.0.0.1:5000
   ```

   Khi branch protection chặn direct push, workflow tạo PR rồi dừng. Merge export-request PR, dispatch lại workflow với `-f release_id=m32-<ORIGINAL_RUN_ID>` cùng model/version. Nếu digest-update PR cũng cần merge, PostSync sẽ chạy sau merge; dispatch lại cùng release ID để xem verification status. Không force push hoặc ghi đè manifest giữa các release.

5. Xác nhận trong S3 `complete.json`, `verified.json`, ECR digest, Git commit, Argo `Synced/Healthy`, `/readyz`, `/model-info`, `/predict` và alias. Chỉ PostSync Job được quyền sửa `champion`.

   ```bash
   RELEASE_ID=m32-123456789  # thay bằng release ID trong workflow summary
   PREFIX="releases/m32/nyc-taxi-trip-duration/v1/$RELEASE_ID"
   aws s3 cp "s3://mlops-aws-model-artifacts-dev-160616607747-ap-southeast-1/$PREFIX/complete.json" -
   aws s3 cp "s3://mlops-aws-model-artifacts-dev-160616607747-ap-southeast-1/$PREFIX/verified.json" -
   ```

## Sự cố và rollback

- Export timeout: xem Application `inference-release-export`, Job logs, Pod Identity association, quyền `mlflow/*` và Registry tags. Chưa có `complete.json` thì workflow không build image.
- ECR push fail: kiểm tra role OIDC subject/audience, repo ID, ECR immutable tag. Nếu image cùng tag đã có, workflow dùng digest hiện có.
- Git push fail: workflow mở PR; branch protection và PR checks có thể cần thao tác merge. GitHub `GITHUB_TOKEN` push không kích hoạt workflow `push` mới, nên các tests quan trọng cũng chạy trong release workflow.
- Argo sync/PostSync fail: không promote model mới. Xem `kubectl -n argocd get application/inference-api -o yaml`, `kubectl -n mlops get jobs`, `kubectl -n mlops logs job/inference-release-verify` và S3 `failed.json`. Revert GitOps commit digest bằng Git revert/PR có kiểm soát, đợi Argo sync về digest cũ; không dùng `kubectl apply` sửa trực tiếp.
- `champion` và S3 `verified.json` không thể cập nhật theo một transaction nguyên tử. Nếu việc ghi status lỗi sau khi MLflow alias đã đổi, kiểm tra alias và logs thủ công rồi chạy lại cùng release ID; promotion idempotent.
- PostSync hook không chạy khi selective sync; luôn sync toàn bộ Application. Nếu image/Pod chưa Healthy, PostSync chưa bắt đầu và alias không đổi.

M3.2 không triển khai HPA, monitoring M3.3, Argo Rollouts hay canary/automatic metric rollback của Phase 4.
