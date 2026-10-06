import { router, useLocalSearchParams } from 'expo-router';
import React, { useEffect, useMemo, useState } from 'react';
import { KeyboardAvoidingView, Platform, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { StationInput } from '@/components/StationInput';
import { Button, Card, Chip, styles as ui } from '@/components/ui';
import { defaultSlot, stepSlot, toIsoDate } from '@/lib/format';
import { useSettings } from '@/lib/settings';
import { colors, fonts, radius } from '@/lib/theme';

export default function SearchScreen() {
  const { t, lang } = useSettings();
  const params = useLocalSearchParams<{ from?: string; to?: string }>();
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [slot, setSlot] = useState(() => defaultSlot(new Date()));
  const { dayOffset, time } = slot;
  const setDayOffset = (d: number) => setSlot((s) => ({ ...s, dayOffset: d }));

  // Opening a saved route fills the form
  useEffect(() => {
    if (params.from) setFrom(params.from);
    if (params.to) setTo(params.to);
  }, [params.from, params.to]);

  const days = useMemo(() => Array.from({ length: 14 }, (_, i) => {
    const d = new Date();
    d.setDate(d.getDate() + i);
    const label = i === 0 ? t('today') : i === 1 ? t('tomorrow')
      : d.toLocaleDateString(lang === 'fr' ? 'fr-FR' : 'en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
    return { iso: toIsoDate(d), label };
  }), [t, lang]);

  const stepTime = (deltaMin: number) => setSlot((s) => stepSlot(s.dayOffset, s.time, deltaMin));

  const canSearch = from.trim().length >= 2 && to.trim().length >= 2;
  const go = () => {
    if (!canSearch) return;
    router.push({ pathname: '/results', params: { from: from.trim(), to: to.trim(), date: days[dayOffset].iso, time } });
  };

  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <ScrollView contentContainerStyle={s.content} keyboardShouldPersistTaps="handled">
        <Text style={s.tagline}>{t('tagline')}</Text>
        <Card style={{ gap: 14 }}>
          <StationInput label={t('from')} value={from} onChange={setFrom} />
          <View style={s.swapRow}>
            <View style={s.rule} />
            <Pressable
              accessibilityRole="button"
              accessibilityLabel={t('swap')}
              onPress={() => { setFrom(to); setTo(from); }}
              style={s.swap}
            >
              <Text style={s.swapText}>⇅</Text>
            </Pressable>
            <View style={s.rule} />
          </View>
          <StationInput label={t('to')} value={to} onChange={setTo} />
        </Card>

        <View>
          <Text style={ui.label}>{t('date')}</Text>
          <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: 8 }} keyboardShouldPersistTaps="handled">
            {days.map((d, i) => <Chip key={d.iso} label={d.label} selected={i === dayOffset} onPress={() => setDayOffset(i)} />)}
          </ScrollView>
        </View>

        <View>
          <Text style={ui.label}>{t('time')}</Text>
          <View style={s.timeRow}>
            <Pressable accessibilityRole="button" accessibilityLabel="-30 min" style={s.step} onPress={() => stepTime(-30)}><Text style={s.stepText}>−</Text></Pressable>
            <Text style={s.timeText}>{time}</Text>
            <Pressable accessibilityRole="button" accessibilityLabel="+30 min" style={s.step} onPress={() => stepTime(30)}><Text style={s.stepText}>+</Text></Pressable>
          </View>
        </View>

        <Button label={t('search')} onPress={go} disabled={!canSearch} />
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const s = StyleSheet.create({
  content: { padding: 16, gap: 18, paddingBottom: 40 },
  tagline: { fontFamily: fonts.heading, fontSize: 24, color: colors.text, letterSpacing: -0.4, lineHeight: 30 },
  swapRow: { flexDirection: 'row', alignItems: 'center', gap: 10 },
  rule: { flex: 1, height: 1, backgroundColor: colors.border },
  swap: { width: 40, height: 40, borderRadius: 20, backgroundColor: colors.accentSoft, alignItems: 'center', justifyContent: 'center' },
  swapText: { fontSize: 18, color: colors.accent },
  timeRow: { flexDirection: 'row', alignItems: 'center', gap: 14 },
  step: { width: 48, height: 48, borderRadius: radius.input, backgroundColor: colors.card, borderWidth: 1, borderColor: colors.border, alignItems: 'center', justifyContent: 'center' },
  stepText: { fontFamily: fonts.heading, fontSize: 22, color: colors.accent },
  timeText: { fontFamily: fonts.heading, fontSize: 32, color: colors.text, minWidth: 100, textAlign: 'center' },
});
