# Phase 5 — Drift Detection & Automated Retraining

**Trạng thái: chưa triển khai.** Phạm vi lấy từ [Proposal v1.0.1](../../MLOps_AWS_Implementation_Plan_v1.0.1.md); không suy diễn kết quả đã chạy.

## 1. Mục tiêu
Phát hiện data/model drift theo batch và kích hoạt lại training có kiểm soát, vẫn phải vượt qua validation gate trước khi release.

## 2. Scope
Drift reports (proposal đề xuất Evidently), metrics/alert, Alertmanager/webhook trigger, duplicate suppression và cooldown. Không thêm training online.

## 3. Kiến trúc dự kiến
Job đọc cửa sổ dữ liệu mới có nhãn phù hợp, so phân phối và chất lượng với baseline, lưu report và phát metric/alert. Trigger tạo training Job trên Spot; candidate đi qua MLflow Tracking/validation gate rồi mới đủ điều kiện release.

## 4. Đã triển khai thực tế
Chưa có drift detector, Alertmanager hay retraining trigger trong repository.

## 5. Luồng dự kiến
Dữ liệu mới → batch drift/quality evaluation → alert có threshold và cooldown → một training Job → Registry candidate → gate → release riêng sau approval. Không tự promote candidate chỉ vì drift.

## 6. Cấu hình và thực thi
Chưa có command vận hành Phase 5. Khi bắt đầu, phải xác định nguồn dữ liệu nhãn mới, baseline và thời hạn dữ liệu trước khi xây report/trigger.

## 7. Cách kiểm chứng dự kiến
Dùng dữ liệu dịch chuyển có kiểm soát, xác nhận report/alert/training Job, chống trigger lặp và kiểm tra candidate bị gate từ chối không được release.

## 8. Kết quả và bằng chứng
Chưa có; Phase 2 mới chứng minh training/gate, Phase 3 chứng minh release/serving.

## 9. Hạn chế và lưu ý
Không nên train lại khi chưa có labels phù hợp. Đo drift trên input không thay thế đánh giá chất lượng model; bổ sung control plane này có thể phát sinh Spot, S3 và monitoring costs.
