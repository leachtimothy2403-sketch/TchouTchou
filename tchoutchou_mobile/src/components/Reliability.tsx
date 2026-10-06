import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { useSettings } from '@/lib/settings';
import { colors, fonts } from '@/lib/theme';
import type { Reliability } from '@/lib/types';
import { ScoreMeter, TierBadge, styles as ui } from './ui';

/** A train's track record: headline on-time %, confidence badge, 95% range and the key
 *  stats -- or the train-type figures, labelled as such, when the train itself is too new.
 *  Shared by the journey detail and the train lookup screens. */
export function ReliabilityPanel({ r }: { r: Reliability | null | undefined }) {
  const { t } = useSettings();
  if (!r) return <Text style={ui.muted}>{t('notEnoughData')}</Text>;
  const h = r.headline;
  const ci = r.confidence.on_time_ci95;
  const own = h.source === 'train';
  const o = r.overall;
  const tf = r.type_fallback;
  return (
    <View style={{ gap: 8 }}>
      <View style={{ flexDirection: 'row' }}>
        <ScoreMeter
          score={h.on_time_pct}
          label={own
            ? `${t('onTime')} · ${t('basedOnTrips', { n: h.observations })}`
            : h.source === 'train_type'
              ? t('basedOnType', { type: h.train_type ?? '', n: h.observations })
              : t('notEnoughData')}
        />
      </View>
      <TierBadge tier={own ? h.tier : h.source === 'train_type' ? 'emerging' : 'insufficient'} />
      {own && ci && <Text style={ui.muted}>{t('range', { lo: ci[0], hi: ci[1] })}</Text>}
      {own && o && (
        <View style={s.stats}>
          <Stat label={t('meanDelay')} value={o.mean_delay_minutes != null ? `${o.mean_delay_minutes} min` : '—'} />
          <Stat label={t('late15')} value={o.late_15_pct != null ? `${o.late_15_pct}%` : '—'} />
          <Stat label={t('cancelled')} value={o.observations ? `${Math.round((o.cancelled_count / (o.observations + o.cancelled_count)) * 100)}%` : '—'} />
        </View>
      )}
      {!own && tf && (
        <View style={s.stats}>
          <Stat label={t('meanDelay')} value={`${tf.mean_delay_minutes} min`} />
          <Stat label={t('late15')} value={`${tf.late_15_pct}%`} />
        </View>
      )}
    </View>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <View style={{ flex: 1 }}>
      <Text style={s.statValue}>{value}</Text>
      <Text style={ui.muted}>{label}</Text>
    </View>
  );
}

const s = StyleSheet.create({
  stats: { flexDirection: 'row', gap: 12, marginTop: 4 },
  statValue: { fontFamily: fonts.heading, fontSize: 18, color: colors.text },
});
