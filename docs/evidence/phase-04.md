# Phase 4 — Bằng chứng kiểm chứng canary

Kiểm chứng ngày **2026-11-05 (UTC)** trên cluster `mlops-aws-dev`, vùng `ap-southeast-1`, Kubernetes 1.36. Tài liệu này chỉ ghi những gì đã quan sát trực tiếp trên cluster; logs thô, mật khẩu và IP cá nhân được giữ ngoài tài liệu public. Thiết kế nằm ở [phase-04-design.md](../phases/phase-04-design.md), vận hành ở [phase-04.md](../phases/phase-04.md).

**Kết luận ngắn.** Canary khỏe được phát hành qua 10% → 50% → 100% và tự promote; canary chậm bị gate độ trễ từ chối và Rollout tự abort; canary không sẵn sàng bị chặn trước khi nhận traffic; sau mỗi lần lỗi, một commit `git revert` đưa Git và cluster về đúng stable trong khoảng 10 giây. **Chưa chứng minh:** release workflow GitHub tự `git revert` (xem mục Giới hạn).

## 1. Cấu hình được kiểm chứng

| Thành phần | Giá trị |
| --- | --- |
| Chia traffic | Argo Rollouts `v1.10.2` (chart 2.43.9) + AWS Load Balancer Controller `v3.6.2`, ALB **internal**, `target-type: ip` |
| Cách nhận workload | `Rollout` dùng `workloadRef` tới `Deployment inference-api`; Deployment về 0 replica sau khi Rollout Healthy |
| Bước canary | canary 1 Pod ở weight 0 + smoke Job → 10% (pause 2 phút) → 50% (pause 2 phút) → 100% |
| Gate chạy nền từ bước 10% | tỷ lệ lỗi `< 1%`; p95 canary `< 0,5 s`; p95 canary `< 1,5 × p95 stable + 50 ms`; tốc độ request canary `>= 0,2 req/s`; mỗi 30 giây, `failureLimit 1`, `inconclusiveLimit 3` |
| Nhánh | Application Argo CD trỏ nhánh thử `m4/canary-tests`; mã nguồn Phase 4 ở nhánh `m4/canary-rollout` (commit `55294e3`, `ad9e8a2`, `7fe2288`, `f4862fe`, `d61a1de`) |
| Tải kiểm thử | k6 chạy **trong cluster** vào ALB internal, 20 VU, `constant-arrival-rate` 40 req/s |

## 2. Hạ tầng và adoption

- Terraform platform: thêm IAM policy (bản chính thức v3.6.2), role và Pod Identity cho LB Controller; node group `system` nâng lên 2 node. Module EKS bỏ qua thay đổi `desired_size` trên node group đang chạy, nên `desired` phải nâng bằng `aws eks update-nodegroup-config` trước khi `min_size = 2` được áp dụng (lần apply đầu lỗi `Minimum capacity 2 can't be greater than desired size 1`).
- `make canary-addons-verify`: LB Controller và Argo Rollouts rollout xong, ba CRD `Established`, IngressClass `alb` có, Pod Identity đã inject.
- Sau khi Argo CD đồng bộ: Rollout `Healthy`, Deployment `0/0`, HPA trỏ `Rollout/inference-api`, hai TargetGroupBinding (stable, canary) được tạo. Argo Rollouts **tự tạo** annotation `alb.ingress.kubernetes.io/actions.inference-api` với weight canary 0 / stable 100. `status.workloadObservedGeneration` trùng `generation` của Deployment (cùng giá trị 4), đúng giả định của `rollout_state`.

## 3. Baseline tải (và một lỗi công cụ)

Lần chạy đầu `M33_RATE=40` ra **92,8 req/s** với p95 494 ms. Log k6 có `"env" level configuration overrode scenarios configuration entirely`: `K6_VUS` và `K6_DURATION` là biến gốc của k6 nên ghi đè toàn bộ `scenarios`, `constant-arrival-rate` bị bỏ qua và tải thực chất là vòng lặp kín bão hòa. Đã đổi sang `LOAD_VUS/RATE/DURATION/BASE_URL`. Hệ quả: các số k6 của Phase 3 (kể cả 55.398 requests) là tải bão hòa theo số VU, không phải tốc độ cố định.

Baseline sau khi sửa (180 giây, 20 VU):

| Chỉ số | Giá trị |
| --- | --- |
| Request | 7.107, **0 lỗi HTTP** |
| Tốc độ | 39,48 req/s |
| Độ trễ | avg 32,66 ms, median 12,88 ms, **p95 32,31 ms**, p90 22,07 ms, max 1,02 s |
| `dropped_iterations` | 94 (khoảng 1,3%, rơi lúc khởi động) |

p95 baseline thấp hơn ngưỡng 500 ms hơn 15 lần nên ngưỡng gate được giữ nguyên.

## 4. T1 — phân phối 90/10 và 50/50

`make canary-split` đọc `increase(inference_http_requests_total{path="/predict"})` theo `rollouts_pod_template_hash`. Chỉ dùng cửa sổ 45 giây nằm hoàn toàn trong một bước (một mẫu 60 giây lấn sang giai đoạn weight 0% bị loại).

| Lần | Bước | Tổng request | Canary | Stable |
| --- | --- | --- | --- | --- |
| Run 1 | 10% | 1.791 | 156 (**8,71%**) | 1.635 (91,29%) |
| Run 1 | 50% | 1.791 | 907,5 (**50,67%**) | 883,5 (49,33%) |
| Run 2 (T2) | 10% | 1.794 | 168 (**9,36%**) | 1.626 (90,64%) |
| Run 2 (T2) | 50% | 1.821 | 936 (**51,40%**) | 885 (48,60%) |

Sai số thống kê khoảng ±0,7 điểm phần trăm ở 10% với cỡ mẫu này; số đo nằm trong sai số của hai lần chạy.

## 5. T2 — canary khỏe

### Run 1: phát hiện lỗi gate (không đạt)
Rollout dừng ở bước cuối với AnalysisRun **`Inconclusive`**: `error-rate` trả `NaN` cả 4 lần (`inconclusive (4) > inconclusiveLimit (3)`). Nguyên nhân: `inference_http_errors_total` không có series cho đến khi có lỗi đầu tiên, nên tử số truy vấn rỗng. Ba gate còn lại `Successful` (p95 canary 25–43 ms; chênh lệch so với giới hạn stable khoảng −0,08; tốc độ canary 2,6–3,9 req/s). Rollout **dừng chờ người** thay vì tự promote khi gate không chắc chắn. Sửa ở commit `f4862fe`: tử số `... or vector(0)`, mẫu số vẫn rỗng khi không có traffic nên "không có request" vẫn là inconclusive; thêm test ghim cả hai hành vi.

### Run 2: sau khi sửa (đạt)
Canary `55cf89fc7b`, chỉ đổi annotation `mlops.aws/release-id` (cùng image, cùng model). Rollout đi qua smoke → 10% → 50% → 100%, kết thúc `Healthy` lúc 09:27:25 với `stableRS = currentPodHash = 55cf89fc7b`, không có thao tác tay. AnalysisRun nền `Successful`:

| Gate | Phép đo cuối | Kết quả |
| --- | --- | --- |
| Tỷ lệ lỗi | 0, 0, 0 | 8 lần đạt |
| p95 canary | 35 ms, 25 ms, 25 ms | 8 lần đạt (ngưỡng 500 ms) |
| p95 so với stable | −0,072, −0,079, −0,063 | 8 lần đạt (cần `< 0`) |
| Tốc độ request canary | 16,7, 20,6, 19,6 req/s | 8 lần đạt |

Mỗi gate có 1 phép đo đầu `NaN` (khởi động, tính là inconclusive, trong giới hạn 3). Smoke Job `Successful`.

## 6. T3 — canary chậm bị từ chối

Canary template đặt `requests.cpu = limits.cpu = 100m` (stable: 250m / 1 CPU); dùng cùng image và model, chỉ thay cấu hình vận hành. Tải 40 req/s qua ALB.

| Mốc (UTC) | Quan sát |
| --- | --- |
| 09:55:19 | Rollouts tạo canary `767c4f9f55` |
| 09:56:08 | Pod Ready sau khoảng 50 giây, smoke `Successful`, Rollout vào 10% |
| 09:57:14 | `phase=Degraded`, `abort=true`, weight canary về 0, stable 100% |

Thông điệp: `RolloutAborted: ... Metric "p95-latency-seconds" assessed Failed due to failed (2) > failureLimit (1)`.

| Gate | Phép đo (sau lần `NaN` đầu) | Kết quả |
| --- | --- | --- |
| p95 canary | 2,158 s, 1,197 s | **Fail** (ngưỡng 0,5 s) |
| p95 so với stable | +2,071, +1,109 | **Fail** (cần `< 0`) |
| Tỷ lệ lỗi | 0, 0 | Đạt: canary chậm nhưng không lỗi |
| Tốc độ request canary | 1,38, 3,68 req/s | Đạt |

Canary bị chặn ngay ở **10%** (dự đoán trước là 50%), tức tối đa khoảng 10% request chạm canary trong khoảng một phút. Stable giữ 3 Pod Ready suốt thời gian đó.

## 7. T4 — canary không sẵn sàng

Hai lần thử trước T3 dùng CPU quá thấp để khởi động: `20m` (canary `7874f4cbfc`, từ 09:29:15) và `40m` (canary `bf7c67b4f`, từ 09:42:51). Cả hai Pod bị `startupProbe` (cửa sổ 120 giây) giết và restart 4 lần dù log có `Application startup complete`. Rollout không qua được bước 0 nên **smoke và gate chưa chạy, canary không nhận request nào**, rồi `Degraded` sau 10 phút với `ProgressDeadlineExceeded` (lần lượt 09:39:17 và 09:52:55). Stable giữ 100% traffic.

Quan sát quan trọng: trong cả hai lần `status.abort` **trống**, chỉ có `phase=Degraded`. `rollout_state` trong `m32_verify.py` xử lý trường hợp này (test ghim ở commit `d61a1de`).

## 8. Khôi phục GitOps bằng `git revert`

| Lần | Commit revert | Từ push đến Rollout `Healthy` | Argo CD sau đó |
| --- | --- | --- | --- |
| Sau T4 (20m) | `1a179cf` | khoảng 10 giây | `Synced/Healthy` ở revision `1a179cf`; Pod canary `Terminating` |
| Sau T3 (100m, 40m) | `2439820`, `119329d` | khoảng 9 giây | `Synced/Healthy` ở revision `119329d`; cờ `abort` được xóa, annotation ALB về canary 0 / stable 100 |

Sau revert, `deployment.yaml` giống hệt bản stable (diff 0 dòng), nên Rollouts không tạo canary mới. Đây là revert **thực hiện bằng tay**; logic tự revert trong `m32_release.sh` chưa chạy thật.

## 9. Giới hạn và điều chưa được chứng minh

- **Tự revert của release workflow chưa chạy thật.** `m32_release.sh` và `m32_verify.py --wait-outcome/--wait-reverted` chỉ được kiểm bằng unit test (phân loại trạng thái, thứ tự revert, không đổi `champion`, RBAC). Workflow chỉ chạy được từ `main` của repo gốc.
- **Nhánh smoke fail chưa được thực thi.** Mọi smoke Job đã chạy đều `Successful`; khi Pod chưa Ready, Rollout dừng trước bước smoke.
- **Gate tỷ lệ lỗi 5xx mới thấy đạt (giá trị 0), chưa thấy fail** với lỗi HTTP thật, vì không chèn lỗi vào ứng dụng (quyết định thiết kế). Truy vấn được kiểm bằng unit test và bằng các phép đo `0` trên cluster.
- **Cỡ mẫu nhỏ:** mỗi phép đo phân phối có khoảng 1.800 request; chưa có kiểm định thống kê, chỉ so với sai số nhị thức ước tính.
- **Môi trường:** account dùng chung, một cluster, tải từ trong cluster; Application trỏ nhánh thử chứ không phải `main`; HPA scale đồng thời (3 Pod stable) nên số Pod stable thay đổi giữa các lần chạy.
- **Thời gian phát hiện lỗi khởi động:** canary không Ready chỉ bị phát hiện sau `progressDeadlineSeconds = 600` (10 phút).
- **Chi phí/mạng:** ALB internal và node `system` thứ hai tính phí theo giờ; ALB phải bị xóa (gỡ Ingress) trước khi hạ VPC.
- Số liệu k6 Phase 3 không so sánh trực tiếp được với các số ở đây (xem mục 3).

## 10. Thay đổi phát sinh từ kiểm chứng

| Phát hiện | Sửa |
| --- | --- |
| `K6_VUS`/`K6_DURATION` ghi đè `scenarios` | Đổi sang biến `LOAD_*`, thêm test cấm tên gốc của k6 (`7fe2288`) |
| `scripts/m42.sh`: `pid` unbound ở `trap EXIT` | Mở rộng PID khi đặt trap (`7fe2288`) |
| Gate `error-rate` luôn `NaN` khi chưa có lỗi | Tử số `or vector(0)` + test (`f4862fe`) |
| `abort` trống khi `ProgressDeadlineExceeded` | Test ghim `Degraded` không cờ `abort` (`d61a1de`) |
| Module EKS bỏ qua `desired_size` | Chú thích trong `eks.tf`, nâng `desired` bằng CLI trước |
| CPU quá thấp làm Pod không khởi động | T3 dùng `100m`; ghi nhận ở T4 |

## 11. Cách lặp lại

```bash
make canary-addons-deploy && make canary-addons-verify
make monitoring-load-test M33_TARGET=alb M33_VUS=20 M33_RATE=40 M33_DURATION_SECONDS=720
make canary-status
make canary-split M42_WINDOW=45s
kubectl -n mlops get analysisrun.argoproj.io
```

Mỗi kịch bản là một commit trên nhánh thử sửa `deploy/gitops/inference/deployment.yaml` (annotation cho T2; `requests`/`limits` CPU cho T3/T4), đẩy lên nhánh mà Application đang trỏ tới; khôi phục bằng `git revert`.
