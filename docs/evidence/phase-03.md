# Phase 3 — bằng chứng serving, GitOps và monitoring

Lần kiểm chứng end-to-end trên dev là **2026-10-23 UTC**. Cloud Registry có approved `nyc-taxi-trip-duration` version 1, run `bb3155725d1149a8bfeefd1a9f184dbf`. [Release workflow run 39826450397](https://github.com/HaiXon231/mlops-platform-aws/actions/runs/39826450397) kết thúc thành công; release đã triển khai dùng bundle artifact SHA256 `d35626fd5e47c1e874be1cb4bb1d04278a33c07a34109cc6df2eccf6a0dcf6b3`, release ID `m32-39826450397-1` và serving digest `sha256:b2a90711ccbd1dd0e3f53b418dc2a39e4963368931da26e50b19e4e52b3536c8`. GitOps release commit [0414040](https://github.com/HaiXon231/mlops-platform-aws/commit/0414040) chứa digest update. Tài liệu này không coi một GitHub source-check run là release run.

Commit cuối cùng được kiểm tra sau monitoring là [686b30d](https://github.com/HaiXon231/mlops-platform-aws/commit/686b30d0602e328dc934e80c0c066daa541d235f); [source checks run 39913759335](https://github.com/HaiXon231/mlops-platform-aws/actions/runs/39913759335) thành công. Argo CD Application `inference-api` ở revision đó `Synced/Healthy`, Deployment 1/1 Ready và image digest không đổi. FastAPI `/readyz` trả `ready`, `/model-info` trả model version 1 và release ID ở trên. `/predict` với pickup 1, dropoff 2, `2025-04-07T16:00:00Z` trả **14,0734827634 phút**. MLflow alias `champion` được đọc trực tiếp từ cloud Registry và vẫn trỏ version 1; không đổi trong bài monitoring.

## HPA và load test

Kubernetes HPA có min1/max3, CPU utilization target 60% của request 250m CPU/Pod, scale-down stabilization 300 giây. k6 Job chạy 30 VUs, requested rate 100 requests/s trong 600 giây, hoàn tất **55.398 requests** (92,29/s thực tế), **0 HTTP failures**, 166.194/166.194 checks pass, P95 k6 402,68 ms.

| UTC | Kết quả quan sát |
| --- | --- |
| 06:28:44 | HPA `SuccessfulRescale` 1→3 Pods vì CPU vượt target |
| 06:32:36–06:38:29 | Deployment 3/3 Ready, HPA CPU khoảng 134% |
| 06:38:24 | k6 Job hoàn thành |
| 06:43:31 | HPA scale 3→2 |
| 06:43:46 | HPA scale 2→1 |
| 06:45:05 | Deployment 1/1 Ready, HPA CPU 1%/60% |

[Mẫu đo CSV](phase-03-hpa-samples.csv) ghi timestamp, HPA và Deployment replicas, CPU. Argo CD vẫn `Synced/Healthy` khi live Deployment có 3 replicas còn Git khai báo initial 1; cấu hình bỏ qua `/spec/replicas` không ghi đè HPA. Ba Pods nằm trên NodeClaim inference On-Demand có sẵn; không có EC2 node mới.

## Quan sát và giới hạn

Prometheus thấy cả ba inference targets `UP=1` trong tải; range query request rate đạt tối đa khoảng **93,87/s**, histogram P95 khoảng **0,487–0,497 giây**. Grafana datasource health `OK`; dashboard có request rate, P95 latency, error rate và Pod scrape status. Hai Services chỉ là ClusterIP. Metrics Server 2 Pods Ready, `kubectl top` và HPA có CPU values. MLflow 1/1 và PostgreSQL 1/1 Ready.

Load test cho thấy hai Pod mới dùng rất ít CPU trong khi Pod gốc đạt gần 1 CPU; traffic nhiều khả năng bám kết nối cũ. Kết quả chứng minh **scale replicas và API tiếp tục phục vụ**, chưa chứng minh throughput tăng nhờ hai Pod mới. Prometheus giữ dữ liệu 24 giờ trên `emptyDir`; metrics mất khi Pod/storage bị thay. Workflow release không được chạy lại trong bài HPA để giữ nguyên model/image/champion.
