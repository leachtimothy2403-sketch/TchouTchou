import { useLocalSearchParams } from 'expo-router';
import React, { useCallback, useEffect, useState } from 'react';
import { ActivityIndicator, KeyboardAvoidingView, Platform as RNPlatform, RefreshControl, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { ReliabilityPanel } from '@/components/Reliability';
import { Button, Card, ErrorPanel, Pill, styles as ui } from '@/components/ui';
import { getTrain } from '@/lib/api';
import { errorMessage } from '@/lib/errors';
import { trainLabel } from '@/lib/format';
import { useSettings } from '@/lib/settings';
import { colors, fonts, radius } from '@/lib/theme';
import type { Platform, StopStatus, TrainBundle } from '@/lib/types';

/** "20261007" -> "07/10" */
const shortDate = (d: string) => (d && d.length === 8 ? `${d.slice(6, 8)}/${d.slice(4, 6)}` : d);

function PlatformBadge({ p }: { p: Platform }) {
  const { t } = useSettings();
  if (p.status === 'Unknown' || !p.platform) return <Text style={ui.muted}>{t('platformUnknown')}</Text>;
  const confirmed = p.status === 'Confirmed';
  return (
    <View style={s.platformRow}>
      <View style={[s.platformBox, confirmed ? s.platformConfirmed : s.platformLikely]}>
        <Text style={[s.platformText, { color: confirmed ? '#fff' : colors.accent }]}>{p.platform}</Text>
      </View>
      <Text style={ui.muted}>
        {confirmed ? t('platformConfirmed') : t('platformLikely', { n: p.based_on_observations ?? 0 })}
      </Text>
    </View>
  );
}

function StopRow({ stop, first }: { stop: StopStatus; first: boolean }) {
  const { t } = useSettings();
  const time = stop.departure_time ?? stop.arrival_time;
  const delay = stop.departure_time ? stop.departure_delay_minutes : stop.arrival_delay_minutes;
  // where you board, the departure platform matters; at the terminus, the arrival one
  const plat = stop.departure_platform.status !== 'Unknown' ? stop.departure_platform : stop.arrival_platform;
  const skipped = stop.schedule_relationship === 'SKIPPED';
  return (
    <View style={[s.stop, !first && s.stopBorder]}>
      <View style={s.stopTimeCol}>
        <Text style={[s.stopTime, skipped && s.strike]}>{time ?? '--:--'}</Text>
        {delay != null && delay > 0 && <Text style={s.delay}>{t('lateNow', { n: delay })}</Text>}
      </View>
      <View style={{ flex: 1, gap: 4 }}>
        <Text style={[s.stopName, skipped && s.strike]}>{stop.station_name ?? '—'}</Text>
        <PlatformBadge p={plat} />
      </View>
    </View>
  );
}

export default function TrainScreen() {
  const { t, apiConfig } = useSettings();
  const params = useLocalSearchParams<{ number?: string }>();
  const [number, setNumber] = useState(params.number ?? '');
  const [data, setData] = useState<TrainBundle | null>(null);
  const [status, setStatus] = useState<'idle' | 'loading' | 'done' | 'error'>('idle');
  const [error, setError] = useState('');
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (n: string, quiet = false) => {
    const q = n.trim();
    if (!q) return;
    if (!quiet) setStatus('loading');
    try {
      setData(await getTrain(apiConfig, q));
      setStatus('done');
    } catch (e) {
      setError(errorMessage(t, e));
      setStatus('error');
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiConfig.baseUrl, apiConfig.appKey, t]);

  // Opened from a journey ("Live status & platforms"): fill in and look up right away
  useEffect(() => {
    if (params.number) {
      setNumber(params.number);
      load(params.number);
    }
  }, [params.number, load]);

  const st = data?.status;
  const delayPill = !st ? null
    : st.cancelled ? <Pill status="bad" text={t('cancelledNow')} />
    : st.delay_minutes == null ? null
    : st.delay_minutes > 0 ? <Pill status={st.delay_minutes >= 15 ? 'bad' : 'ok'} text={t('lateNow', { n: st.delay_minutes })} />
    : <Pill status="good" text={t('onTimeNow')} />;

  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior={RNPlatform.OS === 'ios' ? 'padding' : undefined}>
      <ScrollView
        contentContainerStyle={s.content}
        keyboardShouldPersistTaps="handled"
        refreshControl={st ? <RefreshControl refreshing={refreshing} tintColor={colors.accent}
          onRefresh={async () => { setRefreshing(true); await load(st.train_number, true); setRefreshing(false); }} /> : undefined}
      >
        <View style={{ gap: 6 }}>
          <Text style={ui.label}>{t('trainNumber')}</Text>
          <View style={s.searchRow}>
            <TextInput
              value={number}
              onChangeText={setNumber}
              onSubmitEditing={() => load(number)}
              placeholder="6683"
              placeholderTextColor={colors.muted}
              autoCapitalize="characters"
              autoCorrect={false}
              returnKeyType="search"
              style={s.input}
              accessibilityLabel={t('trainNumber')}
            />
            <Button label={t('lookUp')} onPress={() => load(number)} disabled={!number.trim() || status === 'loading'} style={{ paddingHorizontal: 18, flexShrink: 0 }} />
          </View>
          <Text style={ui.muted}>{t('trainNumberHint')}</Text>
        </View>

        {status === 'loading' && <View style={s.loading}><ActivityIndicator color={colors.accent} /></View>}
        {status === 'error' && <ErrorPanel message={error} onRetry={() => load(number)} />}

        {status === 'done' && st && data && (
          <>
            <Card style={{ gap: 8 }}>
              <View style={s.headRow}>
                <Text style={[ui.h2, { flexShrink: 1 }]}>{trainLabel(st.label) ? `${trainLabel(st.label)} ` : ''}{st.train_number}</Text>
                {delayPill}
              </View>
              {(st.origin || st.destination) && <Text style={ui.body}>{st.origin ?? '?'} → {st.destination ?? '?'}</Text>}
              <Text style={ui.muted}>{t('liveFor', { date: shortDate(st.date) })}</Text>
              {!st.gtfs_tracked && <Text style={ui.muted}>{t('noLiveData')}</Text>}
            </Card>

            {!st.cancelled && st.stops.length > 0 && (
              <Card style={{ paddingVertical: 4 }}>
                <Text style={[ui.label, { marginTop: 12 }]}>{t('stopsTitle')}</Text>
                {st.stops.map((stop, i) => <StopRow key={i} stop={stop} first={i === 0} />)}
              </Card>
            )}

            <Card style={{ gap: 10 }}>
              <Text style={ui.label}>{t('history')}</Text>
              <ReliabilityPanel r={data.reliability} />
            </Card>
          </>
        )}
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const s = StyleSheet.create({
  content: { padding: 16, gap: 14, paddingBottom: 40 },
  searchRow: { flexDirection: 'row', gap: 10, alignItems: 'center' },
  input: { flex: 1, minWidth: 0, width: '100%', fontFamily: fonts.heading, fontSize: 22, color: colors.text, backgroundColor: colors.card, borderWidth: 1, borderColor: colors.border, borderRadius: radius.input, paddingVertical: 10, paddingHorizontal: 14, minHeight: 48 },
  loading: { paddingVertical: 24, alignItems: 'center' },
  headRow: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap' },
  stop: { flexDirection: 'row', gap: 14, paddingVertical: 12 },
  stopBorder: { borderTopWidth: 1, borderTopColor: colors.border },
  stopTimeCol: { width: 58 },
  stopTime: { fontFamily: fonts.heading, fontSize: 18, color: colors.text },
  delay: { fontFamily: fonts.bodyBold, fontSize: 12, color: colors.ok, marginTop: 2 },
  stopName: { fontFamily: fonts.bodyMedium, fontSize: 15, color: colors.text },
  strike: { textDecorationLine: 'line-through', color: colors.muted },
  platformRow: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  platformBox: { minWidth: 30, height: 30, borderRadius: 8, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 6 },
  platformConfirmed: { backgroundColor: colors.accent },
  platformLikely: { backgroundColor: colors.accentSoft, borderWidth: 1, borderColor: colors.accent, borderStyle: 'dashed' },
  platformText: { fontFamily: fonts.heading, fontSize: 16 },
});
