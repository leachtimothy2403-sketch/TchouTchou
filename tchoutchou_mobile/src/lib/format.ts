// Pure helpers (no React / RN imports) so they can be unit-tested with plain node.
import type { Journey, Tier } from './types';

/** "20261010T180400" -> "18:04" (already Paris local time, as SNCF returns it). */
export function hhmm(dt: string | null | undefined): string {
  if (!dt || dt.length < 13) return '--:--';
  return `${dt.slice(9, 11)}:${dt.slice(11, 13)}`;
}

/** "20261010T180400" -> "2026-10-10" */
export function isoDate(dt: string): string {
  return `${dt.slice(0, 4)}-${dt.slice(4, 6)}-${dt.slice(6, 8)}`;
}

export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return '';
  const m = Math.round(seconds / 60);
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return h ? `${h}h${String(mm).padStart(2, '0')}` : `${mm} min`;
}

export function pad(n: number): string {
  return String(n).padStart(2, '0');
}

export function toIsoDate(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function toHHMM(d: Date): string {
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export type Status = 'good' | 'ok' | 'bad' | 'unknown';

/** The one number a journey is judged by -- the same scale for direct trips and trips with
 *  changes: "chance of arriving on time" (see main.py _arrival_on_time_probability). Null
 *  when genuinely unknown, never fabricated. Falls back to the pre-2026-10-07 logic when
 *  talking to an older server that doesn't send the field. */
export function journeyScore(j: Journey): number | null {
  if (j.arrival_on_time_probability !== undefined) return j.arrival_on_time_probability;
  if (j.nb_transfers && j.nb_transfers > 0) return j.combined_success_probability;
  return j.legs[0]?.reliability?.headline.on_time_pct ?? null;
}

/** Tiers from least to most data -- mirrors tchoutchou_api/confidence.py TIERS. */
export const TIER_ORDER: Tier[] = ['insufficient', 'early', 'emerging', 'solid', 'high'];

/** A type-level figure (e.g. "all TER trains") rests on a lot of data but says nothing
 *  about this particular train, so it is capped at this tier. */
const TYPE_LEVEL_CAP = TIER_ORDER.indexOf('emerging');

/** Weakest confidence tier across the legs that carry a number. */
export function journeyTier(j: Journey): Tier {
  if (j.nb_transfers && j.nb_transfers > 0 && j.combined_success_probability == null) {
    return 'insufficient';
  }
  let worst = TIER_ORDER.length - 1;
  for (const leg of j.legs) {
    const h = leg.reliability?.headline;
    if (!h || h.source == null) return 'insufficient';
    // a type-level headline is robust data, but it is not about *this* train
    const own = TIER_ORDER.indexOf(h.tier);
    const idx = h.source === 'train_type' ? Math.min(own, TYPE_LEVEL_CAP) : own;
    worst = Math.min(worst, idx);
  }
  return TIER_ORDER[worst];
}

export function statusOf(score: number | null): Status {
  if (score == null) return 'unknown';
  if (score >= 80) return 'good';
  if (score >= 60) return 'ok';
  return 'bad';
}

/** Value "Most reliable" sorts by: the cautious end of the 95% range when the server sends
 *  it, otherwise the displayed score (older servers). Null = unknown, sorted last. */
export function rankingValue(j: Journey): number | null {
  if (j.ranking_score !== undefined) return j.ranking_score;
  return journeyScore(j);
}

export type SortKey = 'reliability' | 'speed' | 'departure';

export function sortJourneys(list: Journey[], key: SortKey): Journey[] {
  const copy = [...list];
  const rel = (j: Journey) => rankingValue(j);
  if (key === 'reliability') {
    // unknown last; ties broken by faster journey
    copy.sort((a, b) => {
      const ra = rel(a);
      const rb = rel(b);
      if (ra == null && rb == null) return (a.duration_seconds ?? 0) - (b.duration_seconds ?? 0);
      if (ra == null) return 1;
      if (rb == null) return -1;
      if (rb !== ra) return rb - ra;
      return (a.duration_seconds ?? 0) - (b.duration_seconds ?? 0);
    });
  } else if (key === 'speed') {
    copy.sort((a, b) => (a.duration_seconds ?? 1e9) - (b.duration_seconds ?? 1e9));
  } else {
    copy.sort((a, b) => a.departure_datetime.localeCompare(b.departure_datetime));
  }
  return copy;
}

export function tripId(from: string, to: string): string {
  return `${from.trim().toLowerCase()}→${to.trim().toLowerCase()}`;
}

/** Deep link to SNCF Connect's home -- the prefill URL scheme is unverified (see the
 *  API's /deep_link note), so the app only opens the homepage rather than a guessed URL. */
export const SNCF_CONNECT_URL = 'https://www.sncf-connect.com/';

export const MAX_DAY_OFFSET = 13;

/** Default search slot: the next half hour from `now`. Past midnight it rolls to
 *  00:00 *tomorrow* (dayOffset 1) rather than a time that has already gone. */
export function defaultSlot(now: Date): { dayOffset: number; time: string } {
  const d = new Date(now.getTime() + 15 * 60000);
  const sameDay = d.getDate() === now.getDate();
  d.setMinutes(d.getMinutes() < 30 ? 30 : 60, 0, 0);
  const rolled = !sameDay || d.getDate() !== now.getDate();
  return { dayOffset: rolled ? 1 : 0, time: toHHMM(d) };
}

/** Step the departure time by `deltaMin`, carrying across midnight into the day offset.
 *  Clamped to today 00:00 at the low end and the last selectable day 23:xx at the top. */
export function stepSlot(dayOffset: number, time: string, deltaMin: number): { dayOffset: number; time: string } {
  const [h, m] = time.split(':').map(Number);
  let total = h * 60 + m + deltaMin;
  let day = dayOffset;
  while (total >= 1440) { total -= 1440; day += 1; }
  while (total < 0) { total += 1440; day -= 1; }
  if (day < 0) return { dayOffset: 0, time: '00:00' };
  if (day > MAX_DAY_OFFSET) return { dayOffset: MAX_DAY_OFFSET, time: '23:30' };
  return { dayOffset: day, time: `${pad(Math.floor(total / 60))}:${pad(total % 60)}` };
}

/** SIRI-only trains come back labelled with a raw product category ("regionalRail");
 *  show the name a traveller knows. Known GTFS-RT labels ("TGV INOUI", "TER") pass through,
 *  and "service_code X (unmapped)" is internal, so it is hidden. */
const SIRI_CATEGORIES: Record<string, string> = {
  regionalRail: 'TER',
  highSpeedRail: 'TGV',
  interregionalRail: 'Intercités',
  longDistanceRail: 'Intercités',
  suburbanRail: 'Transilien',
  urbanRail: 'RER',
  internationalRail: 'International',
};

export function trainLabel(label: string | null | undefined): string {
  if (!label) return '';
  if (label.startsWith('service_code ')) return '';
  return SIRI_CATEGORIES[label] ?? label;
}
