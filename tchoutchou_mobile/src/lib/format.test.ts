// Run with: npm test   (node's built-in test runner, no extra dependencies)
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { TIER_ORDER, rankingValue, trainLabel, defaultSlot, duration, hhmm, journeyScore, journeyTier, sortJourneys, statusOf, stepSlot, tripId } from './format.ts';
import type { Journey, Leg } from './types.ts';

function leg(headline: any): Leg {
  return {
    train_number: '1', commercial_mode: 'TGV', operator: null, headsign: '1',
    from_station: 'A', to_station: 'B', departure_datetime: '20261010T180400',
    arrival_datetime: '20261010T200200', duration_seconds: 7080,
    reliability: headline && {
      train_number: '1', available: true, confidence: { tier: headline.tier, observations: headline.observations, on_time_ci95: null, thresholds: { emerging: 5, high: 20 } },
      type_fallback: null, headline,
    },
  };
}

function journey(over: Partial<Journey>): Journey {
  return {
    origin: 'A', destination: 'B', departure_datetime: '20261010T180400', arrival_datetime: '20261010T200200',
    duration_seconds: 7080, nb_transfers: 0, status: null, legs: [], transfers: [],
    combined_success_probability: null, combined_probability_notes: [], ...over,
  };
}

test('hhmm / duration', () => {
  assert.equal(hhmm('20261010T180400'), '18:04');
  assert.equal(hhmm(null), '--:--');
  assert.equal(duration(7080), '1h58');
  assert.equal(duration(2400), '40 min');
});

test('direct journey is scored by its train headline', () => {
  const j = journey({ legs: [leg({ source: 'train', on_time_pct: 75, tier: 'high', observations: 40 })] });
  assert.equal(journeyScore(j), 75);
  assert.equal(journeyTier(j), 'high');
});

test('server score is used for ranking when present (direct and with changes on one scale)', () => {
  const L = leg({ source: 'train', on_time_pct: 90, tier: 'high', observations: 60 });
  const change = journey({ nb_transfers: 1, legs: [L, L], combined_success_probability: 90, arrival_on_time_probability: 69.3 });
  const direct = journey({ legs: [leg({ source: 'train', on_time_pct: 77, tier: 'high', observations: 60 })], arrival_on_time_probability: 77 });
  assert.equal(journeyScore(change), 69.3);
  assert.deepEqual(sortJourneys([change, direct], 'reliability'), [direct, change]); // the old bug: 90% connection outranked 77% direct
  assert.equal(journeyScore(journey({ nb_transfers: 1, legs: [L, L], combined_success_probability: 90, arrival_on_time_probability: null })), null);
});

test('older server (no arrival_on_time_probability): combined probability, and null stays null', () => {
  const L = leg({ source: 'train', on_time_pct: 90, tier: 'high', observations: 40 });
  const known = journey({ nb_transfers: 1, legs: [L, L], combined_success_probability: 71.2 });
  assert.equal(journeyScore(known), 71.2);
  const unknown = journey({ nb_transfers: 1, legs: [L, L], combined_success_probability: null });
  assert.equal(journeyScore(unknown), null);
  assert.equal(journeyTier(unknown), 'insufficient');
});

test('a train-type headline caps the tier at emerging', () => {
  const j = journey({ legs: [leg({ source: 'train_type', on_time_pct: 70, tier: 'high', observations: 9000, train_type: 'TER' })] });
  assert.equal(journeyTier(j), 'emerging');
});

test('no reliability data -> insufficient, no score', () => {
  const j = journey({ legs: [leg(null)] });
  assert.equal(journeyScore(j), null);
  assert.equal(journeyTier(j), 'insufficient');
});

test('statusOf thresholds', () => {
  assert.equal(statusOf(null), 'unknown');
  assert.equal(statusOf(95), 'good');
  assert.equal(statusOf(80), 'good');
  assert.equal(statusOf(79.9), 'ok');
  assert.equal(statusOf(59.9), 'bad');
});

test('sorting: reliability puts unknown last; speed and departure work', () => {
  const mk = (id: string, score: number | null, dur: number, dep: string) =>
    journey({ departure_datetime: dep, duration_seconds: dur, legs: [leg(score == null ? null : { source: 'train', on_time_pct: score, tier: 'high', observations: 30 })], origin: id });
  const a = mk('a', 60, 3000, '20261010T190000');
  const b = mk('b', null, 1000, '20261010T170000');
  const c = mk('c', 90, 5000, '20261010T180000');
  assert.deepEqual(sortJourneys([a, b, c], 'reliability').map((j) => j.origin), ['c', 'a', 'b']);
  assert.deepEqual(sortJourneys([a, b, c], 'speed').map((j) => j.origin), ['b', 'a', 'c']);
  assert.deepEqual(sortJourneys([a, b, c], 'departure').map((j) => j.origin), ['b', 'c', 'a']);
});

test('tripId is case/space-insensitive', () => {
  assert.equal(tripId(' Paris ', 'LYON'), tripId('paris', 'lyon'));
});

test('defaultSlot: next half hour, rolling to tomorrow after midnight', () => {
  assert.deepEqual(defaultSlot(new Date(2026, 9, 6, 14, 5)), { dayOffset: 0, time: '14:30' });
  assert.deepEqual(defaultSlot(new Date(2026, 9, 6, 14, 20)), { dayOffset: 0, time: '15:00' });
  assert.deepEqual(defaultSlot(new Date(2026, 9, 6, 23, 13)), { dayOffset: 0, time: '23:30' });
  assert.deepEqual(defaultSlot(new Date(2026, 9, 6, 23, 31)), { dayOffset: 1, time: '00:00' });
  assert.deepEqual(defaultSlot(new Date(2026, 9, 6, 23, 50)), { dayOffset: 1, time: '00:30' });
  assert.deepEqual(defaultSlot(new Date(2026, 9, 31, 23, 40)), { dayOffset: 1, time: '00:00' }); // month end
});

test('stepSlot carries across midnight and clamps', () => {
  assert.deepEqual(stepSlot(0, '23:30', 30), { dayOffset: 1, time: '00:00' });
  assert.deepEqual(stepSlot(1, '00:00', -30), { dayOffset: 0, time: '23:30' });
  assert.deepEqual(stepSlot(0, '00:00', -30), { dayOffset: 0, time: '00:00' });
  assert.deepEqual(stepSlot(13, '23:30', 30), { dayOffset: 13, time: '23:30' });
  assert.deepEqual(stepSlot(2, '10:00', 30), { dayOffset: 2, time: '10:30' });
});

test('five tiers, in the same order as the API', () => {
  assert.deepEqual(TIER_ORDER, ['insufficient', 'early', 'emerging', 'solid', 'high']);
});

test('journeyTier is the weakest leg; a type-level headline never beats "emerging"', () => {
  const mk = (tier: any, source: any = 'train') => leg({ source, on_time_pct: 80, tier, observations: 30, train_type: 'TER' });
  assert.equal(journeyTier(journey({ nb_transfers: 1, combined_success_probability: 70, legs: [mk('high'), mk('solid')] })), 'solid');
  assert.equal(journeyTier(journey({ nb_transfers: 1, combined_success_probability: 70, legs: [mk('high'), mk('early')] })), 'early');
  assert.equal(journeyTier(journey({ legs: [mk('high', 'train_type')] })), 'emerging');
  assert.equal(journeyTier(journey({ legs: [mk('early', 'train_type')] })), 'early');
  assert.equal(journeyTier(journey({ legs: [mk('solid')] })), 'solid');
});

test('trainLabel maps raw SIRI categories, passes known labels, hides internal ones', () => {
  assert.equal(trainLabel('regionalRail'), 'TER');
  assert.equal(trainLabel('TGV INOUI'), 'TGV INOUI');
  assert.equal(trainLabel('service_code 42 (unmapped)'), '');
  assert.equal(trainLabel(null), '');
});

test('"Most reliable" ranks on the cautious end: a well-known 77% beats a lucky-looking 86%', () => {
  const lucky = journey({ origin: 'lucky', legs: [leg({ source: 'train', on_time_pct: 86, tier: 'early', observations: 7 })], arrival_on_time_probability: 86, ranking_score: 48.7 });
  const known = journey({ origin: 'known', legs: [leg({ source: 'train', on_time_pct: 77, tier: 'high', observations: 60 })], arrival_on_time_probability: 77, ranking_score: 65.1 });
  assert.deepEqual(sortJourneys([lucky, known], 'reliability').map((j) => j.origin), ['known', 'lucky']);
  assert.equal(journeyScore(lucky), 86); // the displayed number is unchanged
  assert.equal(rankingValue(lucky), 48.7);
  const unknown = journey({ origin: 'unk', ranking_score: null, arrival_on_time_probability: null });
  assert.deepEqual(sortJourneys([unknown, lucky], 'reliability').map((j) => j.origin), ['lucky', 'unk']);
});

test('older server without ranking_score: ranking falls back to the displayed score', () => {
  const j = journey({ arrival_on_time_probability: 70 });
  assert.equal(rankingValue(j), 70);
});
