import http from 'k6/http';
import { check } from 'k6';

// Không dùng K6_VUS/K6_DURATION: đó là biến gốc của k6 và chúng ghi đè toàn bộ `scenarios`,
// khiến constant-arrival-rate bị bỏ qua. Các biến LOAD_* là của repo này.
const vus = Number(__ENV.LOAD_VUS || 10);
const baseUrl = __ENV.LOAD_BASE_URL || 'http://inference-api.mlops.svc.cluster.local:8000';
// LOAD_PAYLOAD=invalid gửi location ngoài 1..263 nên API trả 422. Chỉ dùng để chứng minh gate
// tỷ lệ lỗi fail khi canary nhận request lỗi; 422 được coi là phản hồi mong đợi để k6 không báo hỏng.
const invalid = (__ENV.LOAD_PAYLOAD || 'valid') === 'invalid';
if (invalid) {
  http.setResponseCallback(http.expectedStatuses(422));
}
export const options = {
  scenarios: {
    inference: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.LOAD_RATE || 20),
      timeUnit: '1s',
      duration: __ENV.LOAD_DURATION || '5m',
      preAllocatedVUs: vus,
      maxVUs: vus,
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.05'],
  },
};

export default function () {
  const response = http.post(
    `${baseUrl}/predict`,
    JSON.stringify({
      pickup_location_id: invalid ? 999 : 1,
      dropoff_location_id: 2,
      pickup_datetime: '2025-04-07T16:00:00Z',
    }),
    { headers: { 'Content-Type': 'application/json' }, timeout: '10s' },
  );
  if (invalid) {
    check(response, { 'rejected with 422': (r) => r.status === 422 });
    return;
  }
  check(response, {
    'prediction 200': (r) => r.status === 200,
    'positive duration': (r) => r.status === 200 && Number(r.json('predicted_duration_minutes')) > 0,
    'registered model version': (r) => r.status === 200 && Number(r.json('model_version')) > 0,
  });
}
