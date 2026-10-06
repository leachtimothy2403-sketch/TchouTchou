import { router, Stack, useLocalSearchParams } from 'expo-router';
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { ActivityIndicator, FlatList, Pressable, StyleSheet, Text, View } from 'react-native';
import { JourneyCard } from '@/components/JourneyCard';
import { Card, Chip, ErrorPanel, styles as ui } from '@/components/ui';
import { searchJourneys } from '@/lib/api';
import { errorMessage } from '@/lib/errors';
import { sortJourneys, type SortKey } from '@/lib/format';
import { setSession } from '@/lib/session';
import { useSettings } from '@/lib/settings';
import { colors, fonts } from '@/lib/theme';
import type { Journey } from '@/lib/types';

export default function ResultsScreen() {
  const { from = '', to = '', date = '', time = '08:00' } = useLocalSearchParams<{ from: string; to: string; date: string; time: string }>();
  const { t, apiConfig, isSaved, toggleSaved } = useSettings();
  const [journeys, setJourneys] = useState<Journey[]>([]);
  const [status, setStatus] = useState<'loading' | 'done' | 'error'>('loading');
  const [error, setError] = useState('');
  const [sort, setSort] = useState<SortKey>('reliability');
  const sessionKey = `${from}|${to}|${date}|${time}`;

  const load = useCallback(async () => {
    setStatus('loading');
    try {
      const r = await searchJourneys(apiConfig, { from, to, date, time });
      setJourneys(r.results);
      setSession(sessionKey, r.results);
      setStatus('done');
    } catch (e) {
      setError(errorMessage(t, e));
      setStatus('error');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [from, to, date, time, apiConfig.baseUrl, apiConfig.appKey]);

  useEffect(() => { load(); }, [load]);

  // Ids index into the *unsorted* session list, so sort a copy that remembers them.
  const sorted = useMemo(() => {
    const tagged = journeys.map((j, i) => ({ j, i }));
    const order = sortJourneys(journeys, sort);
    return order.map((j) => tagged.find((x) => x.j === j)!);
  }, [journeys, sort]);

  const saved = isSaved(from, to);
  const hasRer = journeys.some((j) => j.legs.some((l) => !l.train_number));

  return (
    <>
      <Stack.Screen
        options={{
          headerRight: () => (
            <Pressable accessibilityRole="button" accessibilityLabel={saved ? t('saved') : t('saveTrip')} onPress={() => toggleSaved(from, to)} hitSlop={10}>
              <Text style={{ fontSize: 22, color: saved ? colors.ok : colors.accent }}>{saved ? '★' : '☆'}</Text>
            </Pressable>
          ),
        }}
      />
      <FlatList
        data={status === 'done' ? sorted : []}
        keyExtractor={(x) => String(x.i)}
        contentContainerStyle={s.content}
        ListHeaderComponent={
          <View style={{ gap: 12, marginBottom: 4 }}>
            <View>
              <Text style={ui.h2}>{from} → {to}</Text>
              <Text style={ui.muted}>{date} · {time}</Text>
            </View>
            {status === 'done' && journeys.length > 0 && (
              <View style={s.sortRow}>
                <Chip label={t('sortReliability')} selected={sort === 'reliability'} onPress={() => setSort('reliability')} />
                <Chip label={t('sortSpeed')} selected={sort === 'speed'} onPress={() => setSort('speed')} />
                <Chip label={t('sortDeparture')} selected={sort === 'departure'} onPress={() => setSort('departure')} />
              </View>
            )}
            {status === 'done' && journeys.length > 0 && sort === 'reliability' && (
              <Text style={ui.muted}>{t('sortReliabilityNote')}</Text>
            )}
            {status === 'loading' && (
              <View style={s.loading}><ActivityIndicator color={colors.accent} /><Text style={ui.muted}>{t('searching')}</Text></View>
            )}
            {status === 'error' && <ErrorPanel message={error} onRetry={load} />}
            {status === 'done' && journeys.length === 0 && <Card><Text style={ui.body}>{t('noResults')}</Text></Card>}
          </View>
        }
        renderItem={({ item }) => (
          <JourneyCard
            journey={item.j}
            onPress={() => router.push({ pathname: '/journey', params: { key: sessionKey, id: String(item.i) } })}
          />
        )}
        ItemSeparatorComponent={() => <View style={{ height: 12 }} />}
        ListFooterComponent={status === 'done' && journeys.length > 0 ? (
          <View style={{ gap: 8, marginTop: 16 }}>
            {hasRer && <Text style={ui.muted}>{t('rerNote')}</Text>}
            <Text style={ui.muted}>{t('methodology')}</Text>
          </View>
        ) : null}
      />
    </>
  );
}

const s = StyleSheet.create({
  content: { padding: 16, paddingBottom: 40 },
  sortRow: { flexDirection: 'row', gap: 8, flexWrap: 'wrap' },
  loading: { flexDirection: 'row', gap: 10, alignItems: 'center', paddingVertical: 20 },
});
void fonts;
