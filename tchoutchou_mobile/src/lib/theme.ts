// TrainAware look, carried over from the search-results mockup / static/search.html:
// Space Grotesk (headings, numbers) + IBM Plex Sans (body), #3454d1 accent.
export const colors = {
  accent: '#3454d1',
  accentSoft: '#e8edfb',
  bg: '#f5f6fa',
  card: '#ffffff',
  text: '#14171f',
  muted: '#6a7080',
  border: '#e2e5ee',
  good: '#1f9d63',
  goodSoft: '#e3f5ec',
  ok: '#c98a0b',
  okSoft: '#fbf0d9',
  bad: '#d24a3c',
  badSoft: '#fbe6e3',
  unknown: '#7a8194',
  unknownSoft: '#eceef3',
};

export const fonts = {
  heading: 'SpaceGrotesk_700Bold',
  headingMedium: 'SpaceGrotesk_500Medium',
  body: 'IBMPlexSans_400Regular',
  bodyMedium: 'IBMPlexSans_500Medium',
  bodyBold: 'IBMPlexSans_600SemiBold',
};

export const radius = { card: 16, chip: 999, input: 12 };

export function statusColors(status: 'good' | 'ok' | 'bad' | 'unknown') {
  switch (status) {
    case 'good': return { fg: colors.good, bg: colors.goodSoft };
    case 'ok': return { fg: colors.ok, bg: colors.okSoft };
    case 'bad': return { fg: colors.bad, bg: colors.badSoft };
    default: return { fg: colors.unknown, bg: colors.unknownSoft };
  }
}
