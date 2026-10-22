# M3.2 — Direct GitHub Actions release and Argo CD GitOps

## Release flow

`workflow_dispatch(model_name, model_version)` runs on `main` only. It checks the immutable GitHub repository and owner IDs, tests source, assumes the existing AWS role through OIDC, then temporarily adds the runner's public IPv4 `/32` to the EKS API allowlist. The workflow preserves the original list and both endpoint access modes. The runner uses `kubectl port-forward` to reach **cloud** `svc/mlflow` on localhost:15000. It selects only the requested Registry version, checks `READY` and `validation_status=approved`, downloads the model and training artifacts, verifies signature/schema/prediction parity, and creates a checksummed bundle in the runner workspace. The runner requires S3 read access to `mlflow/*` because MLflow model artifacts use the existing model-artifacts bucket.

One serving image is built and pushed to the existing ECR inference repository. `scripts/m32_gitops.py` pins its real digest and release metadata in `deploy/gitops/inference/deployment.yaml`. The workflow makes **one non-force Git commit** for this release. Argo CD `inference-api` syncs automatically with `selfHeal: true`, `prune: false`. The runner waits for that exact Git revision, a Ready Deployment and the actual Pod imageID, checks `/readyz`, `/model-info` and `/predict`, rechecks Registry approval, then sets or confirms alias `champion` and reads it back. It also confirms `main` and the deployed revision have not changed before promotion. A unique release ID annotation causes a rollout even if an image digest is unchanged.

The release bundle stays on the runner and inside the image. Previous `releases/m32/*` S3 objects and ECR images remain intact as historical evidence; the new workflow neither writes nor waits for them. Export and PostSync Jobs, control image, Kubernetes Lease and a second Git commit are absent from the new release path. Failed verification leaves `champion` unchanged and does not automatically roll back traffic.

## One-time access setup

The EKS cluster has public and private API endpoints enabled and uses Access Entries. Terraform extends the existing GitHub OIDC role with S3 `mlflow/*` read and EKS describe/update permissions restricted to `mlops-aws-dev`, then creates one STANDARD Access Entry in Kubernetes group `mlops-github-release`. The OIDC trust remains pinned to audience `sts.amazonaws.com`, immutable repository ID `1424395869`, owner ID `204581707` and branch `main`. No ClusterAdmin policy is attached. `deploy/bootstrap/m32-github-release-rbac.yaml` grants only the serving/MLflow service and Pod permissions in `mlops`, plus read-only access to the inference Application in `argocd`.

The access entry and namespace RBAC are installed. For Terraform plans, pass the **current baseline** EKS public CIDR list to `TF_VAR_eks_public_access_cidrs`; a stale local IP can otherwise appear as unrelated cluster drift. Keep the pre-existing admin CIDR. No public MLflow Ingress, ALB or broad public Kubernetes API access is needed.

## Run and verify a release

```bash
make m32-test
terraform -chdir=infra/terraform/platform fmt -check
terraform -chdir=infra/terraform/platform validate
kubectl kustomize deploy/gitops/inference
make m32-release M31_MODEL_NAME=nyc-taxi-trip-duration M31_MODEL_VERSION=1
gh run list --workflow m32-release.yml --limit 5
make m32-gitops-verify
```

Use the workflow run page as the evidence chain: explicit Registry name/version/run ID and checksum, ECR image digest, one GitOps commit, Argo CD revision, actual Pod imageID, prediction, and alias read-back. Source checks remain in `m32-ci.yml`. A `GITHUB_TOKEN` push does not start another push workflow, so the release workflow runs its own source tests before building.

The runner's AWS IAM role authenticates AWS calls; the EKS Access Entry maps it to a Kubernetes group; namespace Roles authorize the required `kubectl` operations; and the temporary CIDR allows network reachability. These are independent checks when troubleshooting a connection or `Forbidden` error. MLflow on `127.0.0.1:5000` may be a local Compose server; the release workflow uses its own tunnel on port 15000 and checks `/health` before export.

If branch protection rejects the single non-force commit, the workflow fails with the image digest and commit SHA in its log. No champion change is attempted. Update the repository's approved merge process before dispatching a fresh release; do not force push or promote an image that GitOps has not deployed.

## EKS CIDR recovery

`scripts/m32_eks_cidr.py` writes the original CIDR list to `$RUNNER_TEMP/m32-cidrs.json` **before** requesting an EKS update. It waits for the endpoint update to succeed and checks the temporary list. The final workflow step uses `if: always()` to restore the exact original list and verify both public and private endpoint access remain enabled, even after a failed release step. The GitHub run summary records the original and runner CIDRs. A forced runner termination can prevent any cleanup step from executing, so check EKS after interrupted runs.

For manual recovery, read the **original list from that run's summary**, inspect the live cluster configuration, and restore precisely those original CIDRs using `aws eks update-cluster-config --name mlops-aws-dev --resources-vpc-config` with `endpointPublicAccess=true`, `endpointPrivateAccess=true`, and that exact `publicAccessCidrs` list. Wait for `aws eks describe-update` to report `Successful`, then confirm `aws eks describe-cluster` matches. Never substitute `0.0.0.0/0`, omit the administrator CIDR, or disable private access. The cleanup script refuses to overwrite a different concurrent administrator change and prints the baseline for manual reconciliation. Release workflow concurrency prevents two release runs from changing CIDRs or `champion` simultaneously.

After each release, plan Terraform with the original admin CIDR variable and require no unplanned EKS network change. The successful workflow also reports the restored CIDR list.

M3.2 still excludes HPA/Prometheus/Grafana, ALB, canary traffic and automated metric rollback.
