from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_platform_iam_roles_are_scoped_and_persistent_buckets_are_reused():
    platform = (ROOT / "infra/terraform/platform/mlops.tf").read_text()
    persistent = (ROOT / "infra/terraform/persistent/main.tf").read_text()
    assert 'data "aws_s3_bucket" "mlops"' in platform
    assert 'resource "aws_s3_bucket"' not in platform
    assert '"s3:GetObject"' in platform and '"s3:PutObject"' in platform
    assert '"s3:DeleteObject"' not in platform and '"s3:*"' not in platform
    assert '"aws:RequestTag/kubernetes-service-account"' in platform
    assert 'AmazonEBSCSIDriverEKSClusterScopedPolicy' in platform
    assert 'mlflow    = "mlops-aws-mlflow-' in persistent
    assert 'force_delete         = false' in persistent


def test_teardown_requires_backup_and_restore_verification_before_pvc_delete():
    script = (ROOT / "scripts/m23-teardown.sh").read_text()
    assert script.index('m23-job.sh\" backup') < script.index('m23-job.sh\" verify') < script.index('delete pvc data-postgres-0')
    makefile = (ROOT / "Makefile").read_text()
    target = makefile.split("tf-platform-down:\n", 1)[1].split("\nk8s-bootstrap:", 1)[0]
    assert target.index('m23-teardown') < target.index('k8s-cleanup') < target.index('$(PLATFORM_DIR) destroy')
