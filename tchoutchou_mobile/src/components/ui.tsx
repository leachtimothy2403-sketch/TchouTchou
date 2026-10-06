import React from 'react';
import { Pressable, StyleSheet, Text, View, type StyleProp, type TextStyle, type ViewStyle } from 'react-native';
import { statusOf, TIER_ORDER, type Status } from '@/lib/format';
import { colors, fonts, radius, statusColors } from '@/lib/theme';
import { useSettings } from '@/lib/settings';
import type { Tier } from '@/lib/types';

export function Card({ children, style }: { children: React.ReactNode; style?: StyleProp<ViewStyle> }) {
  return <View style={[styles.card, style]}>{children}</View>;
}

export function Button({ label, onPress, variant = 'primary', disabled, style }: {
  label: string; onPress: () => void; variant?: 'primary' | 'secondary'; disabled?: boolean; style?: StyleProp<ViewStyle>;
}) {
  const primary = variant === 'primary';
  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      disabled={disabled}
      style={({ pressed }) => [
        styles.button,
        primary ? styles.buttonPrimary : styles.buttonSecondary,
        (pressed || disabled) && { opacity: disabled ? 0.45 : 0.8 },
        style,
      ]}
    >
      <Text style={[styles.buttonText, { color: primary ? '#fff' : colors.accent }]}>{label}</Text>
    </Pressable>
  );
}

export function Chip({ label, selected, onPress }: { label: string; selected?: boolean; onPress: () => void }) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ selected: !!selected }}
      onPress={onPress}
      style={[styles.chip, selected && styles.chipSelected]}
    >
      <Text style={[styles.chipText, selected && { color: '#fff' }]}>{label}</Text>
    </Pressable>
  );
}

export function Pill({ text, status, style }: { text: string; status: Status; style?: StyleProp<TextStyle> }) {
  const c = statusColors(status);
  return (
    <View style={[styles.pill, { backgroundColor: c.bg }]}>
      <Text style={[styles.pillText, { color: c.fg }, style]}>{text}</Text>
    </View>
  );
}

/** Big percentage + bar. Unknown renders an honest "Unknown" -- never a fake 0 or 100. */
export function ScoreMeter({ score, label, compact }: { score: number | null; label?: string; compact?: boolean }) {
  const { t } = useSettings();
  const status = statusOf(score);
  const c = statusColors(status);
  return (
    <View style={{ flex: compact ? 0 : 1 }}>
      <Text style={[styles.score, { color: c.fg }, compact && { fontSize: 22 }]}>
        {score == null ? t('unknown') : `${Math.round(score)}%`}
      </Text>
      {label ? <Text style={styles.scoreLabel}>{label}</Text> : null}
      {score != null && (
        <View style={styles.barTrack}>
          <View style={[styles.barFill, { width: `${Math.max(2, Math.min(100, score))}%`, backgroundColor: c.fg }]} />
        </View>
      )}
    </View>
  );
}

export function TierBadge({ tier }: { tier: Tier }) {
  const { t } = useSettings();
  const map: Record<Tier, { label: string; status: Status }> = {
    insufficient: { label: t('tierInsufficient'), status: 'unknown' },
    early: { label: t('tierEarly'), status: 'ok' },
    emerging: { label: t('tierEmerging'), status: 'ok' },
    solid: { label: t('tierSolid'), status: 'good' },
    high: { label: t('tierHigh'), status: 'good' },
  };
  const m = map[tier];
  const c = statusColors(m.status);
  const filled = TIER_ORDER.indexOf(tier) + 1; // 1..5 dots
  return (
    <View style={[styles.tier, { backgroundColor: c.bg }]} accessibilityLabel={`${m.label} (${filled}/${TIER_ORDER.length})`}>
      <View style={{ flexDirection: 'row', gap: 2 }}>
        {TIER_ORDER.map((_, i) => (
          <View key={i} style={[styles.dot, { backgroundColor: i < filled ? c.fg : 'transparent', borderColor: c.fg }]} />
        ))}
      </View>
      <Text style={[styles.tierText, { color: c.fg }]}>{m.label}</Text>
    </View>
  );
}

export function ErrorPanel({ message, onRetry }: { message: string; onRetry?: () => void }) {
  const { t } = useSettings();
  return (
    <Card style={{ borderColor: colors.bad, gap: 10 }}>
      <Text style={[styles.h3, { color: colors.bad }]}>{t('errorTitle')}</Text>
      <Text style={styles.body}>{message}</Text>
      {onRetry ? <Button label={t('retry')} variant="secondary" onPress={onRetry} /> : null}
    </Card>
  );
}

export const styles = StyleSheet.create({
  card: { backgroundColor: colors.card, borderRadius: radius.card, borderWidth: 1, borderColor: colors.border, padding: 16 },
  button: { borderRadius: radius.input, paddingVertical: 14, paddingHorizontal: 18, alignItems: 'center', justifyContent: 'center', minHeight: 48 },
  buttonPrimary: { backgroundColor: colors.accent },
  buttonSecondary: { backgroundColor: colors.accentSoft },
  buttonText: { fontFamily: fonts.bodyBold, fontSize: 16 },
  chip: { paddingVertical: 8, paddingHorizontal: 14, borderRadius: radius.chip, backgroundColor: colors.card, borderWidth: 1, borderColor: colors.border, minHeight: 36, justifyContent: 'center' },
  chipSelected: { backgroundColor: colors.accent, borderColor: colors.accent },
  chipText: { fontFamily: fonts.bodyMedium, fontSize: 14, color: colors.text },
  pill: { alignSelf: 'flex-start', paddingVertical: 3, paddingHorizontal: 10, borderRadius: radius.chip },
  pillText: { fontFamily: fonts.bodyBold, fontSize: 12 },
  score: { fontFamily: fonts.heading, fontSize: 30, letterSpacing: -0.5 },
  scoreLabel: { fontFamily: fonts.body, fontSize: 12, color: colors.muted, marginTop: 1 },
  barTrack: { height: 6, backgroundColor: colors.border, borderRadius: 3, marginTop: 8, overflow: 'hidden' },
  barFill: { height: 6, borderRadius: 3 },
  tier: { flexDirection: 'row', alignItems: 'center', gap: 6, alignSelf: 'flex-start', paddingVertical: 3, paddingHorizontal: 9, borderRadius: radius.chip },
  dot: { width: 7, height: 7, borderRadius: 4, borderWidth: 1 },
  tierText: { fontFamily: fonts.bodyBold, fontSize: 11 },
  h1: { fontFamily: fonts.heading, fontSize: 28, color: colors.text, letterSpacing: -0.5 },
  h2: { fontFamily: fonts.heading, fontSize: 20, color: colors.text },
  h3: { fontFamily: fonts.headingMedium, fontSize: 16, color: colors.text },
  body: { fontFamily: fonts.body, fontSize: 14, color: colors.text, lineHeight: 20 },
  muted: { fontFamily: fonts.body, fontSize: 13, color: colors.muted, lineHeight: 18 },
  label: { fontFamily: fonts.bodyBold, fontSize: 12, color: colors.muted, textTransform: 'uppercase', letterSpacing: 0.6, marginBottom: 6 },
});
