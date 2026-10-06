import {
  IBMPlexSans_400Regular, IBMPlexSans_500Medium, IBMPlexSans_600SemiBold, useFonts as usePlex,
} from '@expo-google-fonts/ibm-plex-sans';
import { SpaceGrotesk_500Medium, SpaceGrotesk_700Bold, useFonts as useGrotesk } from '@expo-google-fonts/space-grotesk';
import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import React from 'react';
import { ActivityIndicator, View } from 'react-native';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import { SettingsProvider, useSettings } from '@/lib/settings';
import { colors, fonts } from '@/lib/theme';

function Shell() {
  const { ready, t } = useSettings();
  const [plexLoaded] = usePlex({ IBMPlexSans_400Regular, IBMPlexSans_500Medium, IBMPlexSans_600SemiBold });
  const [groteskLoaded] = useGrotesk({ SpaceGrotesk_500Medium, SpaceGrotesk_700Bold });

  if (!ready || !plexLoaded || !groteskLoaded) {
    return (
      <View style={{ flex: 1, alignItems: 'center', justifyContent: 'center', backgroundColor: colors.bg }}>
        <ActivityIndicator color={colors.accent} />
      </View>
    );
  }
  return (
    <>
      <StatusBar style="dark" />
      <Stack
        screenOptions={{
          headerStyle: { backgroundColor: colors.bg },
          headerShadowVisible: false,
          headerTintColor: colors.accent,
          headerTitleStyle: { fontFamily: fonts.heading, color: colors.text },
          contentStyle: { backgroundColor: colors.bg },
        }}
      >
        <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
        <Stack.Screen name="results" options={{ title: t('results') }} />
        <Stack.Screen name="journey" options={{ title: t('journeyDetails') }} />
      </Stack>
    </>
  );
}

export default function RootLayout() {
  return (
    <SafeAreaProvider>
      <SettingsProvider>
        <Shell />
      </SettingsProvider>
    </SafeAreaProvider>
  );
}
