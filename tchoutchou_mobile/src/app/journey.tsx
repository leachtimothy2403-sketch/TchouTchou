import { router, Stack, useLocalSearchParams } from 'expo-router';
import React from 'react';
import { Linking, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { ReliabilityPanel } from '@/components/Reliability';
import { Button, Card, Pill, ScoreMeter, TierBadge, styles as ui } from '@/components/ui';
import { duration, hhmm, journeyScore, journeyTier, SNCF_CONNECT_URL, statusOf } from '@/lib/format';
import { getJourney } from '@/lib/session';
import { useSettings } from '@/lib/settings';
import { colors, fonts } from '@/lib/theme';
import type { Leg } from '@/lib/types';

function LegCard({ leg }: { leg: Leg }) {
  const { t } = useSettings();
  return (
    <Card style={{ gap: 12 }}>
      <View style={s.legHead}>
        <Text style={ui.h3}>{leg.commercial_mode || t('leg')} {leg.train_number ?? leg.headsign ?? ''}</Text>
        <Text style={ui.muted}>{duration(leg.duration_seconds)}</Text>
      </View>
      <View style={{ gap: 6 }}>
        <View style={s.stop}><Text style={s.stopTime}>{hhmm(leg.departure_datetime)}</Text><Text style={s.stopName}>{leg.from_station}</Text></View>
        <View style={s.stop}><Text style={s.stopTime}>{hhmm(leg.arrival_datetime)}</Text><Text style={s.stopName}>{leg.to_station}</Text></View>
      </View>
      <View style={s.divider} />
      <Text style={ui.label}>{t('history')}</Text>
      <ReliabilityPanel r={leg.reliability} />
      {leg.train_number ? (
        <Pressable
          accessibilityRole="link"
          onPress={() => router.navigate({ pathname: '/train', params: { number: leg.train_number! } })}
          hitSlop={8}
        >
          <Text style={s.link}>{t('viewTrain')} →</Text>
        </Pressable>
      ) : null}
    </Card>
  );
}

export default function JourneyScreen() {
  const { key = '', id = '0' } = useLocalSearchParams<{ key: string; id: string }>();
  const { t } = useSettings();
  const j = getJourney(key, Number(id));

  if (!j) {
    return <View style={{ padding: 16 }}><Text style={ui.body}>{t('noResults')}</Text></View>;
  }
  const changes = j.nb_transfers ?? j.transfers.length;
  const score = journeyScore(j);

  return (
    <ScrollView contentContainerStyle={s.content}>
      <Stack.Screen options={{ title: `${hhmm(j.departure_datetime)} → ${hhmm(j.arrival_datetime)}` }} />
      <Card style={{ gap: 10 }}>
        <Text style={ui.muted}>{j.origin} → {j.destination} · {duration(j.duration_seconds)}</Text>
        <ScoreMeter score={score} label={changes > 0 ? t('chanceOfMaking') : t('onTime')} />
        <TierBadge tier={journeyTier(j)} />
        {changes > 0 && j.arrival_on_time_probability !== undefined && j.combined_success_probability != null && (
          <Text style={ui.muted}>{t('connectionsOnly', { p: Math.round(j.combined_success_probability) })}</Text>
        )}
        {j.combined_probability_notes.map((n, i) => <Text key={i} style={ui.muted}>{n}</Text>)}
      </Card>

      {j.legs.map((leg, i) => (
        <React.Fragment key={i}>
          <LegCard leg={leg} />
          {j.transfers[i] && (
            <Card style={[s.transfer, { borderColor: statusColorBorder(j.transfers[i].connection_success_probability) }]}>
              <Text style={ui.h3}>{t('transferAt', { station: j.transfers[i].at_station ?? '' })}</Text>
              <Text style={ui.body}>{j.transfers[i].buffer_minutes != null ? t('minutes', { n: j.transfers[i].buffer_minutes! }) : ''}</Text>
              {j.transfers[i].connection_success_probability != null
                ? <Pill status={statusOf(j.transfers[i].connection_success_probability)} text={t('connectionOdds', { p: Math.round(j.transfers[i].connection_success_probability!) })} />
                : <Pill status="unknown" text={t('connectionUnknown')} />}
              {j.transfers[i].note ? <Text style={ui.muted}>{j.transfers[i].note}</Text> : null}
            </Card>
          )}
        </React.Fragment>
      ))}

      {changes > 0 && <Text style={ui.muted}>{t('methodology')}</Text>}
      <View style={{ gap: 6 }}>
        <Button label={t('bookOnSncf')} onPress={() => Linking.openURL(SNCF_CONNECT_URL)} />
        <Text style={[ui.muted, { textAlign: 'center' }]}>{t('bookNote')}</Text>
      </View>
    </ScrollView>
  );
}

function statusColorBorder(p: number | null) {
  const st = statusOf(p);
  return st === 'good' ? colors.good : st === 'ok' ? colors.ok : st === 'bad' ? colors.bad : colors.border;
}

const s = StyleSheet.create({
  content: { padding: 16, gap: 12, paddingBottom: 40 },
  legHead: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  stop: { flexDirection: 'row', gap: 12, alignItems: 'baseline' },
  stopTime: { fontFamily: fonts.heading, fontSize: 18, color: colors.text, width: 56 },
  stopName: { fontFamily: fonts.bodyMedium, fontSize: 15, color: colors.text, flexShrink: 1 },
  divider: { height: 1, backgroundColor: colors.border },
  link: { fontFamily: fonts.bodyBold, fontSize: 14, color: colors.accent },
  transfer: { gap: 8, backgroundColor: colors.accentSoft, borderWidth: 1.5 },
});
