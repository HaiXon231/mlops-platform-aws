TERRAFORM ?= terraform
AWS_PROFILE ?= mlops-dev
BOOTSTRAP_DIR := infra/terraform/bootstrap
PERSISTENT_DIR := infra/terraform/persistent
PLATFORM_DIR := infra/terraform/platform

export AWS_PROFILE
export TF_VAR_aws_profile := $(AWS_PROFILE)

.PHONY: tf-bootstrap-init tf-bootstrap-plan tf-bootstrap-apply \
        tf-persistent-init tf-persistent-plan tf-persistent-apply tf-persistent-verify \
        tf-platform-init tf-platform-plan tf-platform-apply tf-platform-verify tf-platform-down \
        k8s-bootstrap k8s-verify k8s-smoke gitops-smoke k8s-cleanup argocd-port-forward

UV ?= uv
TRAIN_CONFIG ?= configs/training.yaml
MLFLOW_CONFIG ?= configs/mlflow.yaml
MLFLOW_TRACKING_URI ?= http://127.0.0.1:5000
M31_MODEL_NAME ?= nyc-taxi-trip-duration
M31_MODEL_VERSION ?= 1
M31_RELEASE_DIR ?= artifacts/releases/$(M31_MODEL_NAME)-v$(M31_MODEL_VERSION)

.PHONY: m31-model-export m31-test m31-image-build m31-image-push m31-deploy m31-verify m31-cleanup m31-port-forward

.PHONY: m32-test m32-adoption-check m32-gitops-verify m32-promotion-verify m32-release

M32_CLOUD_MLFLOW_URI ?= http://127.0.0.1:15000

m32-test:
	$(UV) run --locked pytest -q tests/unit tests/integration
	$(UV) run --locked python scripts/m31-validate-manifest.py

m32-adoption-check:
	$(UV) run --locked python scripts/m32_adoption.py

m32-gitops-verify:
	kubectl -n argocd get application/inference-api -o wide
	kubectl -n mlops get deployment/inference-api service/inference-api -o wide

m32-promotion-verify:
	$(UV) run --locked python scripts/m32_champion_info.py --tracking-uri "$(M32_CLOUD_MLFLOW_URI)" --model-name "$(M31_MODEL_NAME)"

m32-release:
	gh workflow run m32-release.yml --ref main -f model_name="$(M31_MODEL_NAME)" -f model_version="$(M31_MODEL_VERSION)"

.PHONY: m33-test m33-deploy m33-verify m33-load-test m33-cleanup
M33_VUS ?= 10
M33_RATE ?= 20
M33_DURATION_SECONDS ?= 300

m33-test:
	$(UV) run --locked pytest -q tests/unit/test_m33_config.py
	$(UV) run --locked python scripts/m31-validate-manifest.py
	python3 -m json.tool deploy/monitoring/inference-dashboard.json >/dev/null
	bash -n scripts/m33.sh
	kubectl kustomize deploy/gitops/inference >/dev/null
	helm template m33-prometheus prometheus-community/prometheus --version 29.37.1 --namespace monitoring -f deploy/monitoring/prometheus-values.yaml >/dev/null
	helm template m33-grafana grafana-community/grafana --version 14.0.1 --namespace monitoring -f deploy/monitoring/grafana-values.yaml >/dev/null

m33-deploy:
	bash scripts/m33.sh deploy

m33-verify:
	bash scripts/m33.sh verify

m33-load-test:
	M33_VUS="$(M33_VUS)" M33_RATE="$(M33_RATE)" M33_DURATION_SECONDS="$(M33_DURATION_SECONDS)" bash scripts/m33.sh load-test

m33-cleanup:
	bash scripts/m33.sh cleanup

m31-model-export:
	$(UV) run --locked python -m inference.export --tracking-uri "$(MLFLOW_TRACKING_URI)" --model-name "$(M31_MODEL_NAME)" --model-version "$(M31_MODEL_VERSION)" --output "$(M31_RELEASE_DIR)"

m31-test:
	$(UV) run --locked pytest -q tests/unit tests/integration

m31-image-build:
	M31_RELEASE_DIR="$(M31_RELEASE_DIR)" bash scripts/m31.sh build

m31-image-push:
	M31_RELEASE_DIR="$(M31_RELEASE_DIR)" bash scripts/m31.sh push

m31-deploy:
	bash scripts/m31.sh deploy

m31-verify:
	bash scripts/m31.sh verify

m31-cleanup:
	bash scripts/m31.sh cleanup

m31-port-forward:
	kubectl -n mlops port-forward service/inference-api 8000:8000

.PHONY: training-setup training-test data-download train-local

training-setup:
	$(UV) sync --locked --python 3.12

training-test:
	$(UV) run --locked pytest -q tests/unit tests/integration

data-download:
	$(UV) run --locked mlops-train download --config $(TRAIN_CONFIG)

train-local:
	$(UV) run --locked mlops-train train --config $(TRAIN_CONFIG)

.PHONY: mlflow-up mlflow-down mlflow-verify mlflow-train mlflow-import-run mlflow-validate mlflow-test mlflow-backup mlflow-restore-check

.PHONY: m23-images-build m23-images-push m23-data-sync m23-deploy m23-train m23-backup m23-restore-check m23-restore m23-verify m23-teardown m23-port-forward m23-test

m23-test:
	$(UV) run --locked pytest -q tests/unit tests/integration

m23-images-build:
	bash scripts/m23-images.sh build

m23-images-push:
	bash scripts/m23-images.sh push

m23-data-sync:
	bash scripts/m23-data-sync.sh

m23-deploy:
	bash scripts/m23-deploy.sh

m23-train:
	bash scripts/m23-train.sh

m23-backup:
	bash scripts/m23-job.sh backup

m23-restore-check:
	@test -n "$(BACKUP_KEY)" || { echo 'Set BACKUP_KEY=postgres/<file>.dump'; exit 1; }
	bash scripts/m23-job.sh verify "$(BACKUP_KEY)"

m23-restore:
	@test -n "$(BACKUP_KEY)" || { echo 'Set BACKUP_KEY=postgres/<file>.dump'; exit 1; }
	bash scripts/m23-job.sh restore "$(BACKUP_KEY)"

m23-verify:
	bash scripts/m23-verify.sh

m23-teardown:
	bash scripts/m23-teardown.sh

m23-port-forward:
	kubectl -n mlops port-forward service/mlflow 5000:5000

mlflow-up:
	@test -f .env || { echo 'Create .env from .env.example and set MLFLOW_DB_PASSWORD'; exit 1; }
	docker compose up -d --build --wait

mlflow-down:
	docker compose down

mlflow-verify:
	docker compose ps
	$(UV) run --locked mlops-train tracking-verify --tracking-uri $(MLFLOW_TRACKING_URI)

mlflow-train:
	$(UV) run --locked mlops-train train --config $(TRAIN_CONFIG) --track --mlflow-config $(MLFLOW_CONFIG) --tracking-uri $(MLFLOW_TRACKING_URI)

mlflow-import-run:
	@test -n "$(RUN_DIR)" || { echo 'Set RUN_DIR=artifacts/training/<RUN_ID>'; exit 1; }
	$(UV) run --locked mlops-train import-run --run-dir "$(RUN_DIR)" --mlflow-config $(MLFLOW_CONFIG) --tracking-uri $(MLFLOW_TRACKING_URI)

mlflow-validate:
	@test -n "$(RUN_DIR)" || { echo 'Set RUN_DIR=artifacts/training/<RUN_ID>'; exit 1; }
	$(UV) run --locked mlops-train validate-run --run-dir "$(RUN_DIR)" --mlflow-config $(MLFLOW_CONFIG) --tracking-uri $(MLFLOW_TRACKING_URI)

mlflow-test:
	$(UV) run --locked pytest -q tests/unit tests/integration

mlflow-backup:
	bash scripts/mlflow-backup.sh

mlflow-restore-check:
	@test -n "$(BACKUP_FILE)" || { echo 'Set BACKUP_FILE=backups/<FILE>.dump'; exit 1; }
	bash scripts/mlflow-restore-check.sh "$(BACKUP_FILE)"

tf-bootstrap-init:
	$(TERRAFORM) -chdir=$(BOOTSTRAP_DIR) init

tf-bootstrap-plan:
	$(TERRAFORM) -chdir=$(BOOTSTRAP_DIR) plan

tf-bootstrap-apply:
	$(TERRAFORM) -chdir=$(BOOTSTRAP_DIR) apply

tf-persistent-init:
	@bucket="$$($(TERRAFORM) -chdir=$(BOOTSTRAP_DIR) output -raw state_bucket_name)" || exit $$?; \
	$(TERRAFORM) -chdir=$(PERSISTENT_DIR) init \
	  -backend-config="bucket=$$bucket" \
	  -backend-config="profile=$(AWS_PROFILE)"

tf-persistent-plan:
	$(TERRAFORM) -chdir=$(PERSISTENT_DIR) plan

tf-persistent-apply:
	$(TERRAFORM) -chdir=$(PERSISTENT_DIR) apply

tf-persistent-verify:
	$(TERRAFORM) -chdir=$(PERSISTENT_DIR) validate
	$(TERRAFORM) -chdir=$(PERSISTENT_DIR) plan -detailed-exitcode

tf-platform-init:
	@bucket="$$($(TERRAFORM) -chdir=$(BOOTSTRAP_DIR) output -raw state_bucket_name)" || exit $$?; \
	$(TERRAFORM) -chdir=$(PLATFORM_DIR) init \
	  -backend-config="bucket=$$bucket" \
	  -backend-config="profile=$(AWS_PROFILE)"

tf-platform-plan:
	$(TERRAFORM) -chdir=$(PLATFORM_DIR) plan -input=false

tf-platform-apply:
	$(TERRAFORM) -chdir=$(PLATFORM_DIR) apply

tf-platform-verify:
	$(TERRAFORM) -chdir=$(PLATFORM_DIR) validate
	@set -e; \
	cluster="$$($(TERRAFORM) -chdir=$(PLATFORM_DIR) output -raw eks_cluster_name)" || exit $$?; \
	node_group="$$($(TERRAFORM) -chdir=$(PLATFORM_DIR) output -raw managed_node_group_name)" || exit $$?; \
	kubeconfig="$$(mktemp)" || exit $$?; \
	trap 'rm -f "$$kubeconfig"' EXIT; \
	cluster_status="$$(aws eks describe-cluster --name "$$cluster" --region ap-southeast-1 --query 'cluster.status' --output text)"; \
	node_status="$$(aws eks describe-nodegroup --cluster-name "$$cluster" --nodegroup-name "$$node_group" --region ap-southeast-1 --query 'nodegroup.status' --output text)"; \
	printf 'Cluster: %s; node group: %s\n' "$$cluster_status" "$$node_status"; \
	test "$$cluster_status" = ACTIVE && test "$$node_status" = ACTIVE; \
	aws eks update-kubeconfig --name "$$cluster" --region ap-southeast-1 --kubeconfig "$$kubeconfig" >/dev/null; \
	KUBECONFIG="$$kubeconfig" kubectl wait nodes --all --for=condition=Ready --timeout=180s; \
	KUBECONFIG="$$kubeconfig" kubectl -n kube-system rollout status daemonset/aws-node --timeout=180s; \
	KUBECONFIG="$$kubeconfig" kubectl -n kube-system rollout status daemonset/kube-proxy --timeout=180s; \
	KUBECONFIG="$$kubeconfig" kubectl -n kube-system rollout status deployment/coredns --timeout=180s; \
	KUBECONFIG="$$kubeconfig" kubectl -n kube-system get pods

tf-platform-down:
	@printf 'Type "destroy mlops-aws-dev" to destroy ONLY the platform stack: '; \
	read -r answer; \
	test "$$answer" = "destroy mlops-aws-dev" || { echo "Cancelled."; exit 1; }; \
	$(MAKE) m23-teardown || exit $$?; \
	$(MAKE) k8s-cleanup || exit $$?; \
	$(TERRAFORM) -chdir=$(PLATFORM_DIR) destroy

k8s-bootstrap:
	bash scripts/k8s-bootstrap.sh

k8s-verify:
	bash scripts/k8s-verify.sh

k8s-smoke:
	bash scripts/karpenter-smoke.sh

gitops-smoke:
	bash scripts/gitops-smoke.sh

k8s-cleanup:
	bash scripts/k8s-cleanup.sh

argocd-port-forward:
	kubectl -n argocd port-forward service/argocd-server 8080:443
