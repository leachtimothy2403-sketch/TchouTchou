// Shapes returned by tchoutchou_api (see main.py / confidence.py). Only the fields the
// app reads are typed; the API may return more.

export type Tier = 'insufficient' | 'early' | 'emerging' | 'solid' | 'high';

export interface Confidence {
  tier: Tier;
  level: number; // 0..levels-1
  levels: number;
  observations: number;
  on_time_ci95: [number, number] | null;
  thresholds: Record<string, number>; // lower bound of each tier above 'insufficient'
}

export interface TypeFallback {
  train_type: string;
  n_trains: number;
  observations: number;
  on_time_pct: number;
  mean_delay_minutes: number;
  late_15_pct: number;
}

export interface Headline {
  source: 'train' | 'train_type' | null;
  on_time_pct: number | null;
  tier: Tier;
  observations: number;
  train_type?: string;
}

export interface Overall {
  observations: number;
  cancelled_count: number;
  mean_delay_minutes: number | null;
  on_time_pct: number | null;
  late_15_pct: number | null;
}

export interface Reliability {
  train_number: string;
  available: boolean;
  train_type?: string | null;
  confidence: Confidence;
  type_fallback: TypeFallback | null;
  headline: Headline;
  overall?: Overall | null;
  days_of_history?: number | null;
}

export interface Leg {
  train_number: string | null;
  commercial_mode: string | null;
  operator: string | null;
  headsign: string | null;
  from_station: string | null;
  to_station: string | null;
  departure_datetime: string; // YYYYMMDDTHHMMSS, Paris local time
  arrival_datetime: string;
  duration_seconds: number | null;
  reliability: Reliability | null;
}

export interface Transfer {
  at_station: string | null;
  buffer_minutes: number | null;
  connection_success_probability: number | null;
  note: string | null;
}

export interface Journey {
  origin: string;
  destination: string;
  departure_datetime: string;
  arrival_datetime: string;
  duration_seconds: number | null;
  nb_transfers: number | null;
  status: string | null;
  legs: Leg[];
  transfers: Transfer[];
  combined_success_probability: number | null;
  combined_probability_notes: string[];
  /** One comparable score (API >= 2026-10-07): direct = on-time %, with changes =
   *  connection odds x last train's on-time %. Absent on older servers. */
  arrival_on_time_probability?: number | null;
  /** What "Most reliable" sorts by (API >= 2026-10-07): the same score built from the low
   *  end of each estimate's 95% range, so thin data can't outrank well-known trains. */
  ranking_score?: number | null;
}

export interface SearchResponse {
  from: string;
  to: string;
  date: string;
  time: string;
  results: Journey[];
}

export interface StationHit {
  name: string;
  uic: string | null;
  code: string | null;
  source: 'local' | 'sncf';
}

export interface SavedTrip {
  id: string;
  from: string;
  to: string;
  savedAt: number;
}

export interface Platform {
  status: 'Confirmed' | 'Likely' | 'Unknown';
  platform: string | null;
  based_on_observations?: number;
}

export interface StopStatus {
  station_name: string | null;
  arrival_time: string | null;   // HH:MM, Paris time, already includes delay
  departure_time: string | null;
  arrival_delay_minutes: number | null;
  departure_delay_minutes: number | null;
  schedule_relationship: string | null;
  arrival_platform: Platform;
  departure_platform: Platform;
}

export interface TrainStatus {
  train_number: string;
  date: string; // YYYYMMDD
  label: string | null;
  gtfs_tracked: boolean;
  origin: string | null;
  destination: string | null;
  delay_minutes: number | null;
  cancelled: boolean | null;
  stops: StopStatus[];
}

export interface TrainBundle {
  status: TrainStatus;
  reliability: Reliability;
}
