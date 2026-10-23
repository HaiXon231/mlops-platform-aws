import http from 'k6/http';
import { check } from 'k6';

const vus = Number(__ENV.K6_VUS || 10);
export const options = {
  scenarios: {
    inference: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.K6_RATE || 20),
      timeUnit: '1s',
      duration: __ENV.K6_DURATION || '5m',
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
    'http://inference-api.mlops.svc.cluster.local:8000/predict',
    JSON.stringify({
      pickup_location_id: 1,
      dropoff_location_id: 2,
      pickup_datetime: '2025-04-07T16:00:00Z',
    }),
    { headers: { 'Content-Type': 'application/json' }, timeout: '10s' },
  );
  check(response, {
    'prediction 200': (r) => r.status === 200,
    'positive duration': (r) => r.status === 200 && Number(r.json('predicted_duration_minutes')) > 0,
    'registered model version': (r) => r.status === 200 && Number(r.json('model_version')) > 0,
  });
}
