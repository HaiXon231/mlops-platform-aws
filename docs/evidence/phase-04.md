# Phase 4 — Bằng chứng kiểm chứng canary

Kiểm chứng ngày **2026-11-05 (UTC)** trên cluster `mlops-aws-dev`, vùng `ap-southeast-1`, Kubernetes 1.36. Tài liệu này chỉ ghi những gì đã quan sát trực tiếp trên cluster; logs thô, mật khẩu và IP cá nhân được giữ ngoài tài liệu public. Thiết kế nằm ở [phase-04-design.md](../phases/phase-04-design.md), vận hành ở [phase-04.md](../phases/phase-04.md).

**Kết luận ngắn.** Canary khỏe được phát hành qua 10% → 50% → 100% và tự promote; canary chậm bị gate độ trễ từ chối và Rollout tự abort; canary không sẵn sàng bị chặn trước khi nhận traffic; sau mỗi lần lỗi, một commit `git revert` đưa Git và cluster về đúng stable trong khoảng 10 giây. Release workflow GitHub đã chạy thật trên `main` ở cả hai nhánh: thành công (mục 12) và canary bị từ chối rồi tự `git revert` (mục 13). **Giới hạn chính:** nhánh từ chối mới được thử bằng request không hợp lệ (4xx), chưa bằng lỗi 5xx hay một model lỗi thật (xem mục Giới hạn).

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

- **Nhánh từ chối của release workflow mới được thử với lỗi do tải sinh ra** (mục 13): canary bị loại vì nhận request không hợp lệ, không phải vì chính phiên bản model lỗi, và chỉ chạy một lần. Workflow luôn build image tốt nên không thể tạo canary hỏng thật từ `main` mà không sửa mã hoặc gate.
- **Một số request timeout khi chuyển phiên bản.** Lần tải 15 phút lúc Application chuyển sang `main` (35.931 request, 39,9 req/s, p95 24,9 ms) có **32 request lỗi (0,08%)**, `max` 10 giây tức là timeout, không phải HTTP 5xx do ứng dụng trả (gate `error-rate` vẫn 0 vì ứng dụng chỉ đếm request đã tới nó). Nguyên nhân chưa được kiểm chứng; giả thuyết là Pod cũ bị gỡ khi ALB còn gửi request tới đó. Lỗi này **không lặp lại** trong lần tải suốt release thật (mục 12: 0 lỗi trên 35.921 request), nên chưa thể coi là hệ thống. Hướng xử lý thường dùng nếu lặp lại là `preStop` chờ vài giây và pod readiness gate của LB Controller; chưa áp dụng.
- **Nhánh smoke fail chưa được thực thi.** Mọi smoke Job đã chạy đều `Successful`; khi Pod chưa Ready, Rollout dừng trước bước smoke.
- **Gate tỷ lệ lỗi đã thấy fail, nhưng với 4xx.** Mục 13 cho thấy `error-rate` fail khi canary nhận 422 (gate đếm mọi phản hồi từ 400 trở lên). Chưa thử với 5xx thật, vì không chèn lỗi vào ứng dụng (quyết định thiết kế). Hệ quả cần biết: lỗi của client cũng có thể làm một canary bị từ chối.
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

## 12. Release workflow thật trên `main`

Sau khi PR #1 được merge (`a069afa`), Application Argo CD được trỏ về `main` (`kubectl apply -f deploy/argocd/inference-application-automated.yaml`). Vì annotation `release-id` trên `main` khác nhánh thử, Rollouts mở một canary khỏe cùng image; AnalysisRun `595677df86-9` đạt cả bốn gate (p95 canary 23,5–23,9 ms, so với stable −0,062, lỗi 0, tốc độ 17–20 req/s), Rollout `Healthy`. Rồi workflow `M3.2 approved model release` được dispatch từ `main` với model `nyc-taxi-trip-duration` version `1`.

| Hạng mục | Kết quả |
| --- | --- |
| Run | `42011637577`, dispatch 10:33:05 UTC, kết thúc `success` sau khoảng 12 phút (10:45:12) |
| Các bước | tất cả `success`: kiểm tra danh tính repo, test (`make m32-test`), OIDC, mở CIDR, export/build/deploy/verify, khôi phục CIDR |
| CIDR EKS | ban đầu một IP quản trị `/32`; tạm thêm một IP của runner (xác nhận); sau cùng **khôi phục đúng danh sách ban đầu** (xác nhận) |
| Commit GitOps | đúng một commit của bot, `e235328` `m32: release m32-42011637577-1 by digest` (10:36:14), khoảng 1,5 phút sau khi bước build bắt đầu |
| Canary | AnalysisRun `7f986c76c8-10` và smoke `Successful`; Rollouts trả `{"canary": "promoted"}` lúc 10:44:10 (khoảng 8 phút sau commit) |
| Gate (8 phép đo mỗi gate) | tỷ lệ lỗi 0; p95 canary 22,8–23,0 ms; p95 so với stable −0,061; tốc độ canary 15,5–20,2 req/s |
| Champion | `{"champion": "already-promoted"}` lúc 10:44:39: alias vẫn là model v1, đúng vì release dùng lại model v1 |
| Kết quả cluster | Rollout `Healthy`, stable = current = `7f986c76c8`, 3 Pod Ready; Deployment template mang image digest mới và `release-id: m32-42011637577-1` |

Điều này xác nhận trên hạ tầng thật: RBAC mới của runner đủ để đọc Rollout/AnalysisRun, `rollout_state` nhận đúng trạng thái `promoted`, `main` không bị branch protection chặn bot, và trình tự "một commit GitOps → canary → verify → xác nhận `champion`" hoạt động với canary. Tải k6 được bật ngay khi commit digest xuất hiện (gate cần traffic); không có tải thì `canary-request-rate` là inconclusive. Tải suốt lần release (15 phút, 20 VU, 40 req/s): **35.921 request, 0 lỗi HTTP**, p95 25,47 ms, avg 18,7 ms, max 971 ms, 80 `dropped_iterations`.

## 13. Release workflow thật: canary bị từ chối và tự `git revert`

Mục đích: chứng minh nhánh còn thiếu của `m32_release.sh`. Workflow luôn build image tốt, nên canary được làm hỏng **bằng tải**: ngay khi commit digest của bot xuất hiện, k6 được bật với `LOAD_PAYLOAD=invalid` (`pickup_location_id=999`, API trả 422; 20 VU, 40 req/s, 300 giây). Không sửa gate, không sửa `main` bằng tay. Một lần chạy thử 30 giây trước đó xác nhận k6 hợp lệ (151/151 phản hồi 422 được coi là mong đợi) và `inference_http_errors_total{status="422"}` tăng đúng (khoảng 182 lỗi trên 184 request trong 3 phút).

Run `42016449106`, dispatch 11:08:05 UTC:

| Mốc (UTC) | Quan sát |
| --- | --- |
| 11:08:47–11:09:09 | Mở CIDR cho runner, xác nhận danh sách tạm |
| 11:10:59 | Commit của bot `fc88cfa` `m32: release m32-42016449106-1 by digest`; tải không hợp lệ bật lúc 11:11:00 |
| 11:15:20 | `{"canary": "aborted"}` (4 phút 21 giây sau commit release), workflow ghi `Canary for m32-42016449106-1 was rejected; reverting GitOps commit fc88cfa...` |
| trước 11:15:55 | Commit revert `ffff3ad` `Revert "m32: release m32-42016449106-1 by digest"` lên `main` (thấy lúc 11:15:55) |
| 11:19:07 | `{"rollback": "stable-restored"}`, workflow ghi `Release ... failed its canary gates; stable restored and recorded in Git.` |
| 11:19:41 | Khôi phục CIDR: `["…/32"]` đúng danh sách ban đầu |
| 11:19:44 | Run kết thúc `failure` (khoảng 11 phút 39 giây) |

Khoảng ba phút giữa commit revert và `stable-restored` là do Argo CD hỏi Git mỗi 3 phút, workflow không ép làm mới. Trong thời gian đó canary ở weight 0 (đã abort), stable phục vụ 100%.

AnalysisRun `7f96cdb8c8-11`: `Failed` với `Metric "error-rate" assessed Failed due to failed (2) > failureLimit (1)`; smoke `Successful`.

| Gate | Phép đo | Kết quả |
| --- | --- | --- |
| Tỷ lệ lỗi | `NaN`, `NaN`, **1**, **1** (100%) | **Fail** (ngưỡng 1%) |
| p95 canary | 4,8–5,0 ms | Đạt (422 trả rất nhanh, gate này không phát hiện được kiểu lỗi này) |
| p95 so với stable | −0,052 | Đạt |
| Tốc độ request canary | 0 (trước khi có traffic), 3,3, 3,9 req/s | Một phép đo fail ban đầu, trong giới hạn; kết quả `Successful` |

Kết quả sau cùng:
- Bước **"Export, build, deploy, verify and promote one release"** là bước duy nhất `failure` (có chủ đích); bước khôi phục CIDR `success`.
- `main`: `fc88cfa` rồi `ffff3ad`; `deployment.yaml` trở lại đúng bản stable. Argo CD `Synced/Healthy` ở `ffff3ad`; Rollout `Healthy`, stable = current = `7f986c76c8` (như trước release), cờ `abort` đã xóa.
- `champion` không đổi: log không có bước promote nào (không có dòng `champion`).
- Image bị từ chối (`sha256:8d685581…607d0b88`) vẫn còn trong ECR, không bị xóa.
