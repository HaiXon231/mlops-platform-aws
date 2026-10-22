import os
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
M23 = ROOT / "deploy/platform/mlops"


def render(name, **variables):
    result = subprocess.check_output(
        [sys.executable, str(ROOT / "scripts/m23-render.py"), str(M23 / name)],
        text=True, env={**os.environ, **variables},
    )
    return list(yaml.safe_load_all(result))


def test_storage_postgres_and_mlflow_stay_private_on_system_node():
    storage = yaml.safe_load((M23 / "storageclass.yaml").read_text())
    assert storage["provisioner"] == "ebs.csi.aws.com"
    assert storage["volumeBindingMode"] == "WaitForFirstConsumer"
    assert storage["parameters"]["type"] == "gp3"
    assert storage["reclaimPolicy"] == "Delete"

    service, postgres = list(yaml.safe_load_all((M23 / "postgres.yaml").read_text()))
    assert service["spec"]["type"] == "ClusterIP"
    assert postgres["spec"]["template"]["spec"]["nodeSelector"] == {"eks.amazonaws.com/nodegroup": "system"}
    assert postgres["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"] == "10Gi"
    assert postgres["spec"]["template"]["spec"]["containers"][0]["env"][2]["valueFrom"]["secretKeyRef"]["name"] == "mlflow-db"

    service, mlflow = render("mlflow.yaml.tpl", MLFLOW_IMAGE="example/mlflow:sha", MODEL_BUCKET="models")
    assert service["spec"]["type"] == "ClusterIP"
    pod = mlflow["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"eks.amazonaws.com/nodegroup": "system"}
    assert pod["serviceAccountName"] == "mlflow"
    assert pod["containers"][0]["workingDir"] == "/tmp"
    env = {item["name"]: item for item in pod["containers"][0]["env"]}
    assert env["MLFLOW_ARTIFACTS_DESTINATION"]["value"] == "s3://models/mlflow/"
    allowed_hosts = env["MLFLOW_SERVER_ALLOWED_HOSTS"]["value"].split(",")
    assert "mlflow.mlops.svc.cluster.local:5000" in allowed_hosts
    assert "10.20.*" in allowed_hosts  # Kubelet probes use the pod IP.
    assert "127.0.0.1:*" in allowed_hosts  # Port-forward verification.
    assert "*" not in allowed_hosts
    assert "AWS_ACCESS_KEY_ID" not in env


def test_training_job_uses_spot_and_existing_taint_contract():
    job, = render("training-job.yaml.tpl", RUN_SUFFIX="unit", TRAINING_IMAGE="example/training:sha", DATA_BUCKET="datasets")
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "training"
    assert pod["nodeSelector"] == {"mlops.aws/workload": "training", "karpenter.sh/capacity-type": "spot"}
    assert pod["tolerations"][0]["key"] == "mlops.aws/workload"
    assert pod["tolerations"][0]["value"] == "training"
    assert job["spec"]["backoffLimit"] == 2
    assert job["spec"]["activeDeadlineSeconds"] == 7200
    assert job["spec"]["ttlSecondsAfterFinished"] == 86400


def test_static_system_pod_requests_leave_room_on_t3_large():
    karpenter = yaml.safe_load((ROOT / "deploy/platform/karpenter-values.yaml").read_text())
    argocd = yaml.safe_load((ROOT / "deploy/platform/argocd-values.yaml").read_text())
    _, mlflow = render("mlflow.yaml.tpl", MLFLOW_IMAGE="example/mlflow:sha", MODEL_BUCKET="models")
    _, postgres = list(yaml.safe_load_all((M23 / "postgres.yaml").read_text()))
    own = [mlflow["spec"]["template"]["spec"]["containers"][0], postgres["spec"]["template"]["spec"]["containers"][0]]
    cpu_m = sum(int(item["resources"]["requests"]["cpu"].rstrip("m")) for item in own)
    memory_mi = sum(int(item["resources"]["requests"]["memory"].removesuffix("Mi")) for item in own)
    cpu_m += int(karpenter["controller"]["resources"]["requests"]["cpu"].rstrip("m"))
    memory_mi += int(karpenter["controller"]["resources"]["requests"]["memory"].removesuffix("Mi"))
    for name in ("controller", "server", "repoServer", "applicationSet", "redis"):
        request = argocd[name]["resources"]["requests"]
        cpu_m += int(request["cpu"].rstrip("m"))
        memory_mi += int(request["memory"].removesuffix("Mi"))
    assert (cpu_m, memory_mi) == (850, 1856)
    # EBS CSI, CoreDNS and other system pods need remaining allocatable capacity.
    assert cpu_m < 1200 and memory_mi < 4096


def test_backup_job_uses_dedicated_identity_and_secret():
    job, = render("backup-job.yaml.tpl", BACKUP_JOB_SUFFIX="unit", BACKUP_IMAGE="example/backup:sha", BACKUP_MODE="backup", BACKUP_KEY="", BACKUP_BUCKET="backups")
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "mlflow-backup"
    assert pod["nodeSelector"] == {"eks.amazonaws.com/nodegroup": "system"}
    env = {item["name"]: item for item in pod["containers"][0]["env"]}
    assert env["PGPASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "mlflow-db"
