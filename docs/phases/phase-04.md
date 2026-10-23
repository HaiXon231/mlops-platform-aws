# Phase 4 — Canary Deployment & Automated Rollback

**Trạng thái: chưa triển khai.** Nội dung dưới đây là phạm vi theo [Proposal v1.0.1](../../MLOps_AWS_Implementation_Plan_v1.0.1.md), không phải cấu hình hoặc kết quả đang chạy.

## 1. Mục tiêu
Thử model candidate với một phần traffic thật và tự khôi phục stable version nếu operational metrics không đạt.

## 2. Scope
AWS Load Balancer Controller, ALB, Argo Rollouts, Prometheus AnalysisTemplate, traffic shifting và rollback. Không thay đổi validation gate của Phase 2.

## 3. Kiến trúc dự kiến
Argo Rollouts quản lý stable/canary ReplicaSets; ALB phân chia traffic theo bước; Prometheus cung cấp metric để AnalysisTemplate quyết định tiếp tục hay rollback. GitOps phải nhất quán với stable digest sau rollback.

## 4. Đã triển khai thực tế
Chưa có ALB, Argo Rollouts, canary Service hay AnalysisTemplate trong runtime/repository hiện tại. Phase 3 vẫn dùng một Deployment và ClusterIP.

## 5. Luồng dự kiến
Approved candidate → release image digest → đặt candidate canary → traffic 90/10 → metric analysis → tăng tới 100% nếu đạt hoặc rollback về stable nếu không đạt. Cần thiết kế rõ tương tác với champion alias và GitOps commit trước khi triển khai.

## 6. Cấu hình và thực thi
Chưa có Makefile target hay manifest để chạy Phase 4. Bắt đầu bằng review proposal, capacity/cost ALB và chiến lược giữ stable digest; triển khai ở một thay đổi riêng sau khi review.

## 7. Cách kiểm chứng dự kiến
Gửi request thật để đo phân phối 90/10, kiểm tra metric gates, failure injection và quan sát rollback tự động cùng GitOps state. Không dùng model version giả để tuyên bố hoàn thành.

## 8. Kết quả và bằng chứng
Chưa có. [Evidence Phase 3](../evidence/phase-03.md) chỉ chứng minh serving/HPA, không phải canary.

## 9. Hạn chế và lưu ý
ALB/traffic routing sẽ thêm chi phí và bề mặt mạng. Chưa có cơ chế rollback traffic tự động; lỗi release hiện tại cần vận hành GitOps có kiểm soát.
