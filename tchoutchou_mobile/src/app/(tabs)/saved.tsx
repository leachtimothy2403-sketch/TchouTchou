import { router } from 'expo-router';
import React from 'react';
import { FlatList, Pressable, StyleSheet, Text, View } from 'react-native';
import { Card, styles as ui } from '@/components/ui';
import { useSettings } from '@/lib/settings';
import { colors } from '@/lib/theme';

export default function SavedScreen() {
  const { t, saved, toggleSaved } = useSettings();
  return (
    <FlatList
      data={saved}
      keyExtractor={(x) => x.id}
      contentContainerStyle={s.content}
      ItemSeparatorComponent={() => <View style={{ height: 10 }} />}
      ListEmptyComponent={<Card><Text style={ui.body}>{t('noSaved')}</Text></Card>}
      renderItem={({ item }) => (
        <Pressable accessibilityRole="button" onPress={() => router.navigate({ pathname: '/', params: { from: item.from, to: item.to } })}>
          <Card style={s.row}>
            <View style={{ flex: 1 }}>
              <Text style={ui.h3}>{item.from}</Text>
              <Text style={ui.muted}>→ {item.to}</Text>
            </View>
            <Pressable accessibilityRole="button" accessibilityLabel={t('removeTrip')} hitSlop={10} onPress={() => toggleSaved(item.from, item.to)}>
              <Text style={{ fontSize: 22, color: colors.ok }}>★</Text>
            </Pressable>
          </Card>
        </Pressable>
      )}
    />
  );
}

const s = StyleSheet.create({
  content: { padding: 16, paddingBottom: 40 },
  row: { flexDirection: 'row', alignItems: 'center', gap: 12 },
});
