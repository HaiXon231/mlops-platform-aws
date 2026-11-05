"""Guard the Phase 4 add-on install: pinned charts, Pod Identity wiring and safe cleanup."""

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts/m41.sh").read_text()
COMMON = (ROOT / "scripts/k8s-common.sh").read_text()
MAKEFILE = (ROOT / "Makefile").read_text()


def values(name: str) -> dict:
    return yaml.safe_load((ROOT / "deploy/platform" / name).read_text())


def test_chart_versions_are_pinned_once_and_match_the_make_helm_template_check():
    alb = re.search(r"^ALB_CONTROLLER_CHART_VERSION=(\S+)$", COMMON, re.M).group(1)
    rollouts = re.search(r"^ARGO_ROLLOUTS_CHART_VERSION=(\S+)$", COMMON, re.M).group(1)
    assert re.fullmatch(r"\d+\.\d+\.\d+", alb) and re.fullmatch(r"\d+\.\d+\.\d+", rollouts)
    assert f"--version {alb} --namespace kube-system" in MAKEFILE
    assert f"--version {rollouts} --namespace argo-rollouts" in MAKEFILE
    assert '"$ALB_CONTROLLER_CHART_VERSION"' in SCRIPT and '"$ARGO_ROLLOUTS_CHART_VERSION"' in SCRIPT
    # The policy file in Terraform is pinned to the same controller release as the chart.
    assert (ROOT / f"infra/terraform/platform/policies/aws-load-balancer-controller-v{alb}.json").exists()


def test_controller_uses_pod_identity_service_account_that_terraform_trusts():
    config = values("aws-load-balancer-controller-values.yaml")
    terraform = (ROOT / "infra/terraform/platform/alb_controller.tf").read_text()
    name = config["serviceAccount"]["name"]
    assert name == "aws-load-balancer-controller"
    assert f'alb_controller_service_account = "{name}"' in terraform
    assert "annotations" not in config["serviceAccount"], "Pod Identity needs no IRSA annotation"
    assert 'identifiers = ["pods.eks.amazonaws.com"]' in terraform
    assert 'values   = ["kube-system"]' in terraform
    assert config["ingressClass"] == "alb" and config["createIngressClassResource"] is True
    assert config["nodeSelector"] == {"eks.amazonaws.com/nodegroup": "system"}
    assert not any(config[key] for key in ("enableShield", "enableWaf", "enableWafv2"))
    # Cluster name, region and VPC are passed explicitly; pods must not rely on IMDS.
    for flag in ("clusterName=$CLUSTER_NAME", "region=$AWS_REGION", "vpcId=$vpc_id"):
        assert f'--set-string "{flag}"' in SCRIPT


def test_rollouts_controller_is_small_and_has_no_dashboard():
    config = values("argo-rollouts-values.yaml")
    assert config["installCRDs"] is True
    assert config["controller"]["replicas"] == 1
    assert config["dashboard"]["enabled"] is False
    assert config["controller"]["nodeSelector"] == {"eks.amazonaws.com/nodegroup": "system"}
    assert config["controller"]["resources"]["requests"]["cpu"] == "50m"


def test_deploy_requires_applied_iam_and_never_touches_the_inference_workload():
    deploy = SCRIPT[SCRIPT.index("deploy() {"):SCRIPT.index("verify() {")]
    assert "tf_output alb_controller_role_arn" in deploy
    assert "-alb-controller" in deploy
    assert "kubectl delete" not in SCRIPT and "-n mlops" not in SCRIPT
    assert "helm upgrade --install" in deploy


def test_cleanup_refuses_while_alb_ingress_or_rollouts_exist():
    cleanup = SCRIPT[SCRIPT.index("cleanup() {"):SCRIPT.index("case ")]
    assert cleanup.index("Refusing cleanup") < cleanup.index("helm -n")
    assert 'spec.ingressClassName=="alb"' in cleanup
    assert "rollouts.argoproj.io" in cleanup


def test_make_targets_and_script_syntax():
    for target in ("m41-test", "m41-deploy", "m41-verify", "m41-cleanup",
                   "canary-addons-test", "canary-addons-deploy", "canary-addons-verify", "canary-addons-cleanup"):
        assert re.search(rf"^{target}:", MAKEFILE, re.M), target
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts/m41.sh")]).returncode == 0
