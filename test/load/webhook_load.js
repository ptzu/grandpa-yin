// k6 load test — floods POST /webhook with valid-signature LINE text events.
//
// ⚠️ STAGING ONLY. Do NOT point this at production:
//   - It hits the real DB write path (state lookups, member checks, logs).
//   - The bot will try to reply to LINE with the FAKE replyTokens below and
//     those calls fail (expected). This test measures INGESTION + DB pool +
//     dedup under load — NOT message delivery.
//   - To also test the AI worker pool / graceful "busy" degradation, run
//     against a staging deploy with Replicate mocked (see README).
//
// It reproduces LINE's signature so requests pass handler.parser.parse:
//   X-Line-Signature = base64( HMAC-SHA256( CHANNEL_SECRET, rawBody ) )
//
// Usage:
//   k6 run -e TARGET=https://staging-app.up.railway.app \
//          -e CHANNEL_SECRET=xxxxxxxx \
//          -e MODE=spike test/load/webhook_load.js

import http from 'k6/http';
import crypto from 'k6/crypto';
import { check } from 'k6';

const TARGET = __ENV.TARGET || 'http://localhost:8080';
const CHANNEL_SECRET = __ENV.CHANNEL_SECRET || '';
const MODE = __ENV.MODE || 'steady';

const PROFILES = {
  steady: [
    { duration: '30s', target: 15 },
    { duration: '2m', target: 15 },
    { duration: '30s', target: 0 },
  ],
  spike: [
    { duration: '10s', target: 5 },
    { duration: '10s', target: 120 },  // community-post burst
    { duration: '1m', target: 120 },
    { duration: '20s', target: 5 },
    { duration: '10s', target: 0 },
  ],
  soak: [
    { duration: '1m', target: 10 },
    { duration: '30m', target: 10 },
    { duration: '1m', target: 0 },
  ],
};

export const options = {
  stages: PROFILES[MODE] || PROFILES.steady,
  thresholds: {
    // The webhook should accept fast (it enqueues heavy work, returns 200).
    http_req_failed: ['rate<0.02'],
    http_req_duration: ['p(95)<1000'],
  },
};

export function setup() {
  if (!CHANNEL_SECRET) {
    throw new Error('CHANNEL_SECRET is required. Pass -e CHANNEL_SECRET=...');
  }
  if (/localhost|127\.0\.0\.1|staging|test/i.test(TARGET) === false) {
    // Loud guard against accidentally flooding production.
    console.warn(
      `\n⚠️  TARGET does not look like a staging/local URL:\n    ${TARGET}\n` +
      `    This writes to the real DB and triggers failing LINE replies.\n` +
      `    Ctrl+C now if this is production.\n`
    );
  }
}

// Build one LINE text-message webhook payload. Every request gets a unique
// webhookEventId so the in-memory dedup does NOT drop them (we want real load,
// not deduped no-ops).
function buildEvent() {
  const uid = `Uload${__VU}${__ITER}`.padEnd(33, '0').slice(0, 33);
  const eid = `evt-${__VU}-${__ITER}-${Date.now()}`;
  return JSON.stringify({
    destination: 'xxxxxxxxxx',
    events: [
      {
        type: 'message',
        mode: 'active',
        timestamp: Date.now(),
        webhookEventId: eid,
        source: { type: 'user', userId: uid },
        replyToken: '00000000000000000000000000000000', // fake — reply will fail (expected)
        message: { type: 'text', id: `${Date.now()}`, text: '你好' },
      },
    ],
  });
}

export default function () {
  const body = buildEvent();
  const signature = crypto.hmac('sha256', CHANNEL_SECRET, body, 'base64');
  const res = http.post(`${TARGET}/webhook`, body, {
    headers: {
      'Content-Type': 'application/json',
      'X-Line-Signature': signature,
    },
  });
  check(res, {
    'accepted (200)': (r) => r.status === 200,
    'not a signature reject (400)': (r) => r.status !== 400,
    'not a server error (5xx)': (r) => r.status < 500,
  });
}
