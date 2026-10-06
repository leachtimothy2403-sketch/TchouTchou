import React, { useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { searchStations } from '@/lib/api';
import { useSettings } from '@/lib/settings';
import { colors, fonts, radius } from '@/lib/theme';
import type { StationHit } from '@/lib/types';
import { styles as ui } from './ui';

/** Text field with inline station suggestions (rendered in the flow, not as a floating
 *  overlay, so it behaves the same on iOS, Android and web and never fights the
 *  keyboard). Suggestions are best-effort -- typing any name still works, because
 *  /api/search resolves free text itself. */
export function StationInput({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  const { t, apiConfig } = useSettings();
  const [hits, setHits] = useState<StationHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [focused, setFocused] = useState(false);
  const seq = useRef(0);
  const picked = useRef<string | null>(null);

  useEffect(() => {
    const q = value.trim();
    if (!focused || q.length < 2 || picked.current === q) {
      setHits([]);
      setLoading(false);
      return;
    }
    const my = ++seq.current;
    setLoading(true);
    const timer = setTimeout(async () => {
      try {
        const r = await searchStations(apiConfig, q);
        if (my === seq.current) setHits(r);
      } catch {
        if (my === seq.current) setHits([]);
      } finally {
        if (my === seq.current) setLoading(false);
      }
    }, 250);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, focused, apiConfig.baseUrl, apiConfig.appKey]);

  return (
    <View>
      <Text style={ui.label}>{label}</Text>
      <View style={s.inputWrap}>
        <TextInput
          value={value}
          onChangeText={(v) => { picked.current = null; onChange(v); }}
          onFocus={() => setFocused(true)}
          onBlur={() => setTimeout(() => setFocused(false), 150)}
          placeholder={t('stationPlaceholder')}
          placeholderTextColor={colors.muted}
          autoCorrect={false}
          autoCapitalize="words"
          returnKeyType="next"
          style={s.input}
          accessibilityLabel={label}
        />
        {loading ? <ActivityIndicator size="small" color={colors.accent} style={{ marginRight: 12 }} /> : null}
      </View>
      {focused && hits.length > 0 && (
        <View style={s.list}>
          {hits.map((h, i) => (
            <Pressable
              key={`${h.name}-${i}`}
              onPress={() => { picked.current = h.name; onChange(h.name); setHits([]); }}
              style={({ pressed }) => [s.row, i > 0 && s.rowBorder, pressed && { backgroundColor: colors.accentSoft }]}
            >
              <Text style={s.rowText}>{h.name}</Text>
              {h.code ? <Text style={s.code}>{h.code}</Text> : null}
            </Pressable>
          ))}
        </View>
      )}
    </View>
  );
}

const s = StyleSheet.create({
  inputWrap: { flexDirection: 'row', alignItems: 'center', backgroundColor: colors.card, borderWidth: 1, borderColor: colors.border, borderRadius: radius.input },
  input: { flex: 1, fontFamily: fonts.bodyMedium, fontSize: 17, color: colors.text, paddingVertical: 13, paddingHorizontal: 14 },
  list: { marginTop: 6, backgroundColor: colors.card, borderWidth: 1, borderColor: colors.border, borderRadius: radius.input, overflow: 'hidden' },
  row: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', paddingVertical: 12, paddingHorizontal: 14 },
  rowBorder: { borderTopWidth: 1, borderTopColor: colors.border },
  rowText: { fontFamily: fonts.bodyMedium, fontSize: 15, color: colors.text, flexShrink: 1 },
  code: { fontFamily: fonts.bodyBold, fontSize: 12, color: colors.muted, marginLeft: 8 },
});
