import React, { useState } from 'react';
import { ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { Button, Card, Chip, styles as ui } from '@/components/ui';
import { health, normalizeBaseUrl } from '@/lib/api';
import { errorMessage } from '@/lib/errors';
import { useSettings } from '@/lib/settings';
import { colors, fonts, radius } from '@/lib/theme';

export default function SettingsScreen() {
  const { t, langChoice, apiUrl, appKey, apiConfig, update } = useSettings();
  const [url, setUrl] = useState(apiUrl);
  const [key, setKey] = useState(appKey);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const test = async () => {
    const cfg = { ...apiConfig, baseUrl: url, appKey: key };
    setBusy(true);
    setResult(null);
    try {
      const h = await health(cfg);
      const when = h.stats_updated_at_utc ? h.stats_updated_at_utc.slice(0, 16).replace('T', ' ') + ' UTC' : '—';
      setResult({ ok: h.ok, text: t('connectionOk', { when }) });
      update({ apiUrl: normalizeBaseUrl(url), appKey: key.trim() });
    } catch (e) {
      setResult({ ok: false, text: `${t('connectionFail')} ${errorMessage(t, e)}` });
    } finally {
      setBusy(false);
    }
  };

  return (
    <ScrollView contentContainerStyle={s.content} keyboardShouldPersistTaps="handled">
      <Card style={{ gap: 10 }}>
        <Text style={ui.label}>{t('language')}</Text>
        <View style={s.row}>
          <Chip label={t('langAuto')} selected={langChoice === 'auto'} onPress={() => update({ langChoice: 'auto' })} />
          <Chip label="Français" selected={langChoice === 'fr'} onPress={() => update({ langChoice: 'fr' })} />
          <Chip label="English" selected={langChoice === 'en'} onPress={() => update({ langChoice: 'en' })} />
        </View>
      </Card>

      <Card style={{ gap: 10 }}>
        <Text style={ui.label}>{t('serverUrl')}</Text>
        <TextInput value={url} onChangeText={setUrl} autoCapitalize="none" autoCorrect={false} keyboardType="url"
          placeholder="https://xxxx.trycloudflare.com" placeholderTextColor={colors.muted} style={s.input} accessibilityLabel={t('serverUrl')} />
        <Text style={ui.label}>{t('appKey')}</Text>
        <TextInput value={key} onChangeText={setKey} autoCapitalize="none" autoCorrect={false} secureTextEntry
          placeholderTextColor={colors.muted} style={s.input} accessibilityLabel={t('appKey')} />
        <Text style={ui.muted}>{t('appKeyHint')}</Text>
        <Button label={busy ? t('searching') : t('testConnection')} onPress={test} disabled={busy || !url.trim()} />
        {result && <Text style={[ui.body, { color: result.ok ? colors.good : colors.bad }]}>{result.text}</Text>}
      </Card>

      <Card style={{ gap: 6 }}>
        <Text style={ui.label}>{t('about')}</Text>
        <Text style={ui.muted}>{t('aboutText')}</Text>
      </Card>
    </ScrollView>
  );
}

const s = StyleSheet.create({
  content: { padding: 16, gap: 14, paddingBottom: 40 },
  row: { flexDirection: 'row', gap: 8, flexWrap: 'wrap' },
  input: { fontFamily: fonts.bodyMedium, fontSize: 16, color: colors.text, borderWidth: 1, borderColor: colors.border, borderRadius: radius.input, paddingVertical: 12, paddingHorizontal: 14, backgroundColor: colors.bg },
});
