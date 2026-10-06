// Thin client for tchoutchou_api. No React imports so it is testable with plain node.
import type { SearchResponse, StationHit, TrainBundle } from './types';

export type ApiErrorKind = 'network' | 'auth' | 'rate' | 'sncf' | 'notfound' | 'http';

export class ApiError extends Error {
  kind: ApiErrorKind;
  status: number;
  retryAfter?: number;

  constructor(kind: ApiErrorKind, status: number, message: string, retryAfter?: number) {
    super(message);
    this.kind = kind;
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

export interface ApiConfig {
  baseUrl: string;
  appKey: string;
  deviceId: string;
  lang: string;
}

export function normalizeBaseUrl(url: string): string {
  let u = (url || '').trim();
  if (!u) return '';
  if (!/^https?:\/\//i.test(u)) u = `https://${u}`;
  return u.replace(/\/+$/, '');
}

async function request<T>(cfg: ApiConfig, path: string, params: Record<string, string | number | undefined>, timeoutMs = 20000): Promise<T> {
  const base = normalizeBaseUrl(cfg.baseUrl);
  if (!base) throw new ApiError('network', 0, 'No server address configured');
  const qs = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .join('&');
  const headers: Record<string, string> = { Accept: 'application/json', 'X-Device-Id': cfg.deviceId, 'Accept-Language': cfg.lang };
  if (cfg.appKey) headers['X-App-Key'] = cfg.appKey;

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(`${base}${path}${qs ? `?${qs}` : ''}`, { headers, signal: ctrl.signal });
  } catch (e) {
    throw new ApiError('network', 0, String(e));
  } finally {
    clearTimeout(timer);
  }

  let body: any = null;
  try {
    body = await res.json();
  } catch {
    /* non-JSON body (e.g. a tunnel error page) */
  }
  if (res.ok) {
    if (body == null) throw new ApiError('http', res.status, 'Server returned a non-JSON response');
    return body as T;
  }
  const detail = typeof body?.detail === 'string' ? body.detail : `HTTP ${res.status}`;
  if (res.status === 401) throw new ApiError('auth', 401, detail);
  if (res.status === 404) throw new ApiError('notfound', 404, detail);
  if (res.status === 429) throw new ApiError('rate', 429, detail, body?.retry_after_seconds);
  if (res.status === 502) throw new ApiError('sncf', 502, detail);
  // Cloudflare tunnel 52x/530 = the origin (the VPS API) is down
  if (res.status >= 520 && res.status <= 530) throw new ApiError('network', res.status, detail);
  throw new ApiError('http', res.status, detail);
}

export function searchJourneys(cfg: ApiConfig, p: { from: string; to: string; date: string; time: string }) {
  return request<SearchResponse>(cfg, '/api/search', { ...p, count: 6 }, 30000);
}

export async function searchStations(cfg: ApiConfig, q: string): Promise<StationHit[]> {
  const r = await request<{ results: StationHit[] }>(cfg, '/api/stations', { q, limit: 6 }, 8000);
  return r.results;
}

export interface Health {
  ok: boolean;
  version: string;
  stats_updated_at_utc?: string | null;
}

export function health(cfg: ApiConfig) {
  return request<Health>(cfg, '/api/health', {}, 8000);
}

/** Live status + track record for one train (GET /api/trains/{number}). */
export function getTrain(cfg: ApiConfig, trainNumber: string) {
  return request<TrainBundle>(cfg, `/api/trains/${encodeURIComponent(trainNumber.trim())}`, {}, 15000);
}
