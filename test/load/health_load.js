// k6 load test — hammers GET /health.
//
// WHY /health: it runs a real DB query (check_connection) but triggers NO
// outbound LINE reply and NO Replicate call. So it exercises the exact thing
// we worry about at launch — the shared DB connection pool and app memory —
// with zero cost and zero side effects. Safe to point at production.
//
// Usage:
//   k6 run -e TARGET=https://your-app.up.railway.app test/load/health_load.js
//   k6 run -e TARGET=... -e MODE=spike test/load/health_load.js
//   k6 run -e TARGET=... -e MODE=soak  test/load/health_load.js
//   k6 run -e TARGET=... -e MODE=flood -e RATE=500 -e DURATION=1m test/load/health_load.js
//
// While it runs, watch (this is the point of the test):
//   - Supabase dashboard → Database → connection count (must stay < 60 on Nano/Micro)
//   - Railway → Metrics → RAM / CPU
//   - The k6 summary below: p95 latency, http_req_failed rate, http_reqs/s

import http from 'k6/http';
import { check } from 'k6';

const TARGET = __ENV.TARGET || 'http://localhost:8080';
const MODE = __ENV.MODE || 'steady';

// CLOSED model (steady/spike/soak): a fixed pool of VUs, each waits for its
// response before firing again. Self-throttling — if the server slows, the VUs
// slow with it. Good for a safe, gentle ceiling probe.
//   steady — sustained expected load, sanity + latency baseline
//   spike  — sudden burst, mimics a community post dropping (the launch risk)
//   soak   — long steady run, surfaces memory / connection leaks
const PROFILES = {
  steady: [
    { duration: '30s', target: 20 },  // ramp up
    { duration: '2m', target: 20 },   // hold
    { duration: '30s', target: 0 },   // ramp down
  ],
  spike: [
    { duration: '10s', target: 5 },    // calm before
    { duration: '10s', target: 150 },  // sudden burst
    { duration: '1m', target: 150 },   // sustained peak
    { duration: '20s', target: 5 },    // recover
    { duration: '10s', target: 0 },
  ],
  soak: [
    { duration: '1m', target: 15 },
    { duration: '30m', target: 15 },   // hold long enough to expose leaks
    { duration: '1m', target: 0 },
  ],
};

const thresholds = {
  // Fail the test if the service degrades past these — tune to taste.
  http_req_failed: ['rate<0.01'],       // < 1% errors
  http_req_duration: ['p(95)<800'],     // 95% of requests under 800ms
};

// OPEN model (flood): requests arrive at a fixed rate regardless of whether
// the server keeps up. This does NOT self-throttle — if the box drowns, the
// backlog piles up and k6 either sees timeouts/503s or reports dropped
// iterations (it couldn't even launch the request). This is the realistic
// "everyone floods in and keeps tapping" scenario, harsher than spike.
//   -e RATE=500       requests per second to force
//   -e DURATION=1m    how long to sustain it
const RATE = parseInt(__ENV.RATE || '500', 10);
const DURATION = __ENV.DURATION || '1m';

export const options = MODE === 'flood'
  ? {
      scenarios: {
        flood: {
          executor: 'constant-arrival-rate',
          rate: RATE,
          timeUnit: '1s',
          duration: DURATION,
          preAllocatedVUs: 200,
          maxVUs: 1000,          // let k6 add VUs to hold the rate as latency rises
        },
      },
      thresholds,
    }
  : {
      stages: PROFILES[MODE] || PROFILES.steady,
      thresholds,
    };

export default function () {
  const res = http.get(`${TARGET}/health`);
  check(res, {
    'status is 200': (r) => r.status === 200,
    'db healthy': (r) => {
      try {
        return JSON.parse(r.body).checks.database === true;
      } catch (_) {
        return false;
      }
    },
  });
}
