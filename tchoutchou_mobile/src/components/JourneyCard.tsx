import React from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { duration, hhmm, journeyScore, journeyTier, statusOf } from '@/lib/format';
import { useSettings } from '@/lib/settings';
import { colors, fonts } from '@/lib/theme';
import type { Journey } from '@/lib/types';
import { Card, Pill, ScoreMeter, TierBadge, styles as ui } from './ui';

export function JourneyCard({ journey, onPress }: { journey: Journey; onPress: () => void }) {
  const { t } = useSettings();
  const changes = journey.nb_transfers ?? journey.transfers.length;
  const score = journeyScore(journey);
  const tier = journeyTier(journey);
  const first = journey.legs[0];
  const headline = first?.reliability?.headline;
  const tightest = journey.transfers.reduce<number | null>(
    (m, x) => (x.buffer_minutes == null ? m : m == null ? x.buffer_minutes : Math.min(m, x.buffer_minutes)), null);

  const scoreLabel = changes > 0
    ? t('chanceOfMaking')
    : headline?.source === 'train_type'
      ? t('basedOnType', { type: headline.train_type ?? '', n: headline.observations })
      : headline?.source === 'train'
        ? `${t('onTime')} · ${t('basedOnTrips', { n: headline.observations })}`
        : t('notEnoughData');

  return (
    <Pressable onPress={onPress} accessibilityRole="button" style={({ pressed }) => pressed && { opacity: 0.85 }}>
      <Card style={{ gap: 12 }}>
        <View style={s.timesRow}>
          <Text style={s.time}>{hhmm(journey.departure_datetime)}</Text>
          <View style={s.line}><View style={s.lineBar} /></View>
          <Text style={s.time}>{hhmm(journey.arrival_datetime)}</Text>
        </View>
        <View style={s.metaRow}>
          <Text style={ui.muted}>
            {duration(journey.duration_seconds)} · {changes === 0 ? t('direct') : changes === 1 ? t('change') : t('changes', { n: changes })}
          </Text>
          <Text style={ui.muted} numberOfLines={1}>
            {journey.legs.map((l) => l.commercial_mode || l.train_number || '·').join(' → ')}
          </Text>
        </View>
        <View style={s.scoreRow}>
          <ScoreMeter score={score} label={scoreLabel} />
        </View>
        <View style={s.footer}>
          <TierBadge tier={tier} />
          {tightest != null && tightest < 8 && <Pill text={`${t('tightWarning')} · ${t('minutes', { n: tightest })}`} status={statusOf(40)} />}
        </View>
      </Card>
    </Pressable>
  );
}

const s = StyleSheet.create({
  timesRow: { flexDirection: 'row', alignItems: 'center', gap: 10 },
  time: { fontFamily: fonts.heading, fontSize: 24, color: colors.text, letterSpacing: -0.5 },
  line: { flex: 1, justifyContent: 'center' },
  lineBar: { height: 2, backgroundColor: colors.border, borderRadius: 1 },
  metaRow: { flexDirection: 'row', justifyContent: 'space-between', gap: 12 },
  scoreRow: { flexDirection: 'row' },
  footer: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap' },
});
