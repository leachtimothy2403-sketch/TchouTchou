import { Tabs } from 'expo-router';
import React from 'react';
import { Text, type ColorValue } from 'react-native';
import { useSettings } from '@/lib/settings';
import { colors, fonts } from '@/lib/theme';

// Plain glyphs instead of an icon font: nothing extra to load, nothing to break offline.
const glyph = (g: string) => ({ color, focused }: { color: ColorValue; focused: boolean }) =>
  <Text style={{ color, fontSize: 20, opacity: focused ? 1 : 0.8 }}>{g}</Text>;

export default function TabsLayout() {
  const { t } = useSettings();
  return (
    <Tabs
      screenOptions={{
        headerStyle: { backgroundColor: colors.bg },
        headerShadowVisible: false,
        headerTitleStyle: { fontFamily: fonts.heading, color: colors.text },
        tabBarActiveTintColor: colors.accent,
        tabBarInactiveTintColor: colors.muted,
        tabBarLabelStyle: { fontFamily: fonts.bodyMedium, fontSize: 12 },
        tabBarStyle: { backgroundColor: colors.card, borderTopColor: colors.border, height: 68, paddingTop: 6, paddingBottom: 8 },
      }}
    >
      <Tabs.Screen name="index" options={{ title: t('tabSearch'), headerTitle: t('appName'), tabBarIcon: glyph('⌕') }} />
      <Tabs.Screen name="train" options={{ title: t('tabTrain'), tabBarIcon: glyph('🚆') }} />
      <Tabs.Screen name="saved" options={{ title: t('tabSaved'), tabBarIcon: glyph('★') }} />
      <Tabs.Screen name="settings" options={{ title: t('tabSettings'), tabBarIcon: glyph('⚙') }} />
    </Tabs>
  );
}
