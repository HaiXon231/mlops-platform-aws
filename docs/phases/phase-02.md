# Phase 2 — Data & Model Training Pipeline

## 1. Mục tiêu
Huấn luyện model NYC Taxi có thể tái hiện, đánh giá đúng train/validation/test, quản lý run và approved versions bằng MLflow, rồi chạy cùng logic trên EKS Spot.

## 2. Scope
NYC TLC Parquet, SHA256/metadata, LightGBM, feature contract, MLflow Tracking/Registry, validation gate, cloud training Job, S3 artifacts, PostgreSQL backup/restore. Không bao gồm tự động release serving hoặc retraining theo drift.

## 3. Kiến trúc và thành phần
`src/training/` chứa data, features, model, gate, tracking và CLI. `configs/training.yaml` chọn train 2025-01/02, validation 2025-03, test 2025-04, seed 42 và tối đa 100.000 dòng/tháng sau lọc. LightGBM dùng đúng năm features: `PULocationID`, `DOLocationID` (categorical 1–263), `pickup_hour`, `pickup_day_of_week`, `pickup_month`. Model artifacts/schema/manifest lưu theo run; cloud MLflow dùng PostgreSQL PVC và S3 Model Artifacts.

## 4. Đã triển khai
Local training, Compose MLflow và EKS Spot Job dùng cùng pipeline. Gate chỉ đăng ký candidate được duyệt; rejected run vẫn hiện trong experiment. Cloud Registry có approved `nyc-taxi-trip-duration` version 1. S3 Datasets sync xác minh checksum, training/MLflow/backup Pods dùng Pod Identity, không cần static AWS keys. PostgreSQL dump, restore check và cơ chế restore vào DB mới đã có scripts.

## 5. Luồng hoạt động
`make data-download` → kiểm tra metadata → `make cloud-data-sync` → Spot Job tải đúng bốn tháng → cleaning/sampling/features → LightGBM train/early stopping → MAE/RMSE → validation gate so candidate cùng dataset/metric → log MLflow run → nếu đạt thì đăng ký version. Test split chỉ báo cáo, không tham gia gate. MLflow cloud khác Compose local.

## 6. Cấu hình và thực thi
`make training-setup`, `make training-test`, `make data-download`, `make train-local` dùng local. Cloud cần Phase 1, `make cloud-images-build`, `make cloud-images-push`, `make cloud-data-sync`, `make cloud-deploy` với `MLFLOW_DB_PASSWORD`, rồi `make cloud-train`. Chi tiết thứ tự và quyền ở [setup](../setup.md).

## 7. Kiểm chứng
`make cloud-verify` kiểm tra CSI, PostgreSQL/PVC, MLflow health và Tracking API; `VERIFY_TRAINING=1 make cloud-verify` còn kiểm tra Job, registered model và Spot NodeClaims được thu hồi. `make cloud-backup`/`cloud-restore-check BACKUP_KEY=...` thử dump vào DB tạm và so metadata mà không ghi DB chính. Unit/integration tests kiểm tra feature parity, gate và tracking.

## 8. Kết quả và bằng chứng
[Evidence Phase 2](../evidence/phase-02.md) ghi validation MAE 3,8372 phút, test MAE 4,1301 phút của local artifact; cloud version 1/run ID và giới hạn provenance. Không suy ra mọi bản Parquet đã được xác minh từ nguồn vì manifest local có `source_verified=false`.

## 9. Hạn chế vận hành
PostgreSQL một replica/PVC không HA. Dump database không chứa model S3 artifacts; giữ cả hai bucket khi phục hồi. Spot có thể thiếu capacity; giữ training Job retry/timeout và kiểm tra NodeClaims. Chỉ restore primary vào DB rỗng, không ghi đè DB đang phục vụ.
