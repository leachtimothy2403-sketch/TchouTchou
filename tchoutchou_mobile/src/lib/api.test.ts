import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ApiError, getTrain, normalizeBaseUrl, searchJourneys } from './api.ts';

const cfg = { baseUrl: 'https://x.trycloudflare.com/', appKey: 'k1', deviceId: 'device-12345678', lang: 'fr' };

function mockFetch(status: number, body: any) {
  const calls: any[] = [];
  (globalThis as any).fetch = async (url: string, init: any) => {
    calls.push({ url, init });
    return { ok: status < 400, status, json: async () => { if (body === undefined) throw new Error('no json'); return body; } };
  };
  return calls;
}

test('normalizeBaseUrl', () => {
  assert.equal(normalizeBaseUrl('x.trycloudflare.com/'), 'https://x.trycloudflare.com');
  assert.equal(normalizeBaseUrl(' http://192.168.1.5:8000// '), 'http://192.168.1.5:8000');
  assert.equal(normalizeBaseUrl(''), '');
});

test('sends key + device headers and encodes params', async () => {
  const calls = mockFetch(200, { results: [] });
  await searchJourneys(cfg, { from: 'Saint-Étienne', to: 'Lyon Part-Dieu', date: '2026-10-10', time: '08:00' });
  assert.match(calls[0].url, /^https:\/\/x\.trycloudflare\.com\/api\/search\?from=Saint-%C3%89tienne&to=Lyon%20Part-Dieu&date=2026-10-10&time=08%3A00&count=6$/);
  assert.equal(calls[0].init.headers['X-App-Key'], 'k1');
  assert.equal(calls[0].init.headers['X-Device-Id'], 'device-12345678');
});

test('omits X-App-Key when none configured', async () => {
  const calls = mockFetch(200, { results: [] });
  await searchJourneys({ ...cfg, appKey: '' }, { from: 'a', to: 'b', date: '2026-10-10', time: '08:00' });
  assert.equal('X-App-Key' in calls[0].init.headers, false);
});

test('maps HTTP failures to typed errors', async () => {
  const run = async (status: number, body: any) => {
    mockFetch(status, body);
    try { await searchJourneys(cfg, { from: 'a', to: 'b', date: 'd', time: 't' }); } catch (e) { return e as ApiError; }
    throw new Error('expected failure');
  };
  assert.equal((await run(401, { detail: 'bad key' })).kind, 'auth');
  const rate = await run(429, { detail: 'slow down', retry_after_seconds: 30 });
  assert.equal(rate.kind, 'rate'); assert.equal(rate.retryAfter, 30);
  const sncf = await run(502, { detail: 'SNCF API rate limit hit' });
  assert.equal(sncf.kind, 'sncf'); assert.match(sncf.message, /rate limit/);
  assert.equal((await run(530, undefined)).kind, 'network');
  assert.equal((await run(500, undefined)).kind, 'http');
});

test('network failure and missing server address', async () => {
  (globalThis as any).fetch = async () => { throw new TypeError('Network request failed'); };
  await assert.rejects(searchJourneys(cfg, { from: 'a', to: 'b', date: 'd', time: 't' }), (e: ApiError) => e.kind === 'network');
  await assert.rejects(searchJourneys({ ...cfg, baseUrl: '' }, { from: 'a', to: 'b', date: 'd', time: 't' }), (e: ApiError) => e.kind === 'network');
});

test('getTrain: path-encodes the number, 404 -> notfound', async () => {
  const calls = mockFetch(200, { status: {}, reliability: {} });
  await getTrain(cfg, ' 6683 ');
  assert.equal(calls[0].url, 'https://x.trycloudflare.com/api/trains/6683');
  mockFetch(404, { detail: 'No data found for train 1' });
  await assert.rejects(getTrain(cfg, '1'), (e: ApiError) => e.kind === 'notfound');
});
