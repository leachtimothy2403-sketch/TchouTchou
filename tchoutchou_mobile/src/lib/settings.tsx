// App-wide state: language, server address, app key, device id, saved routes. Persisted
// with AsyncStorage; every read/write is guarded so the app still works if storage fails.
import AsyncStorage from '@react-native-async-storage/async-storage';
import Constants from 'expo-constants';
import * as Crypto from 'expo-crypto';
import { getLocales } from 'expo-localization';
import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ApiConfig } from './api';
import { detectLang, translate, type Key, type Lang } from './i18n';
import type { SavedTrip } from './types';
import { tripId } from './format';

const KEY = 'trainaware.v1';

interface Persisted {
  langChoice: 'auto' | Lang;
  apiUrl: string;
  appKey: string;
  deviceId: string;
  saved: SavedTrip[];
}

const extra = (Constants.expoConfig?.extra ?? {}) as { apiUrl?: string; appKey?: string };
const defaults = (): Persisted => ({
  langChoice: 'auto',
  apiUrl: process.env.EXPO_PUBLIC_API_URL || extra.apiUrl || '',
  appKey: process.env.EXPO_PUBLIC_APP_KEY || extra.appKey || '',
  deviceId: Crypto.randomUUID(),
  saved: [],
});

interface Ctx {
  ready: boolean;
  lang: Lang;
  langChoice: 'auto' | Lang;
  t: (key: Key, vars?: Record<string, string | number>) => string;
  apiConfig: ApiConfig;
  apiUrl: string;
  appKey: string;
  saved: SavedTrip[];
  update: (patch: Partial<Pick<Persisted, 'langChoice' | 'apiUrl' | 'appKey'>>) => void;
  toggleSaved: (from: string, to: string) => void;
  isSaved: (from: string, to: string) => boolean;
}

const SettingsContext = createContext<Ctx | null>(null);

export function SettingsProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<Persisted>(defaults);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const raw = await AsyncStorage.getItem(KEY);
        if (raw) {
          const stored = JSON.parse(raw) as Partial<Persisted>;
          setState((s) => ({ ...s, ...stored, deviceId: stored.deviceId || s.deviceId }));
        }
      } catch {
        /* first run, or storage unavailable */
      }
      setReady(true);
    })();
  }, []);

  const persist = useCallback((next: Persisted) => {
    AsyncStorage.setItem(KEY, JSON.stringify(next)).catch(() => {});
  }, []);

  // make sure a freshly generated device id sticks after first load
  useEffect(() => {
    if (ready) persist(state);
  }, [ready, state, persist]);

  const lang: Lang = state.langChoice === 'auto' ? detectLang(getLocales()[0]?.languageTag) : state.langChoice;

  const value = useMemo<Ctx>(() => ({
    ready,
    lang,
    langChoice: state.langChoice,
    t: (key, vars) => translate(lang, key, vars),
    apiConfig: { baseUrl: state.apiUrl, appKey: state.appKey, deviceId: state.deviceId, lang },
    apiUrl: state.apiUrl,
    appKey: state.appKey,
    saved: state.saved,
    update: (patch) => setState((s) => ({ ...s, ...patch })),
    toggleSaved: (from, to) => setState((s) => {
      const id = tripId(from, to);
      const exists = s.saved.some((x) => x.id === id);
      const saved = exists ? s.saved.filter((x) => x.id !== id) : [{ id, from, to, savedAt: Date.now() }, ...s.saved];
      return { ...s, saved };
    }),
    isSaved: (from, to) => state.saved.some((x) => x.id === tripId(from, to)),
  }), [ready, lang, state]);

  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>;
}

export function useSettings(): Ctx {
  const c = useContext(SettingsContext);
  if (!c) throw new Error('useSettings must be used inside <SettingsProvider>');
  return c;
}
