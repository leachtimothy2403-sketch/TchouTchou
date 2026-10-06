// Route params can't carry objects, so the results screen parks the journeys it fetched
// here and the detail screen reads one back by index. In-memory only (cleared on app
// restart), which is fine: opening a journey always follows a search in the same run.
import type { Journey } from './types';

let current: { key: string; journeys: Journey[] } = { key: '', journeys: [] };

export function setSession(key: string, journeys: Journey[]) {
  current = { key, journeys };
}

export function getJourney(key: string, id: number): Journey | null {
  return current.key === key ? current.journeys[id] ?? null : null;
}
