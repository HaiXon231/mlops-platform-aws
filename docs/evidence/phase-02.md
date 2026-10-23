# Phase 2 — bằng chứng training và Registry

Nguồn số liệu đánh giá: local artifact Git-ignored `artifacts/training/20261021T092102070842Z/metrics.json` và `manifest.json` được đọc khi tổng hợp tài liệu. SHA256 của hai file lần lượt là `c3cef1d6cfff88794ac8f29593409f801be2bfe4b0bb3e4c0f43186764a57f26` và `5b278e164d05ed5270c74094814abcfba2120e7d53311e251288c5d6427b593f`. Bốn tháng 2025-01/02/03/04 đều được lấy mẫu 100.000 dòng sau lọc; manifest của run local này ghi `source_verified=false` cho bốn file cache cũ. SHA256 chỉ kiểm tra tính toàn vẹn file đã có, **không chứng minh provenance từ NYC TLC**.

| Tập | Model MAE (phút) | Model RMSE (phút) | Median baseline MAE (phút) | Rows |
| --- | ---: | ---: | ---: | ---: |
| Validation 2025-03 | 3,8372435071 | 6,5290651786 | 7,9246943333 | 100.000 |
| Test 2025-04 | 4,1301360316 | 6,9192082073 | 8,4226165 | 100.000 |

Cloud MLflow Registry được kiểm tra ngày 2026-10-23: registered model `nyc-taxi-trip-duration` version **1** được `validation_status=approved`; alias `champion` trỏ đến version 1, MLflow run `bb3155725d1149a8bfeefd1a9f184dbf`. Đây là trạng thái cloud đọc được, không phải Registry local Compose. Training Job trên Spot, artifact S3, PostgreSQL backup và restore check đã được thực hiện trong giai đoạn triển khai; repository hiện giữ script/verification logic. Không có raw Job log hay backup dump trong Git để độc lập tái kiểm tra các lần chạy cũ. Để kiểm tra hiện trạng, dùng `VERIFY_TRAINING=1 make cloud-verify`, `make cloud-backup` và `make cloud-restore-check BACKUP_KEY=...` với một key thực; các lệnh backup tạo/đọc S3 objects và không nên chạy chỉ để đọc evidence.

Validation gate và feature pipeline có unit/integration tests trong `tests/unit`, `tests/integration`. Full suite được chạy lại ngày 2026-10-23: **54 tests pass**, bốn dependency warnings đã có trước đó. Test local không thay thế kiểm chứng cloud Registry.
