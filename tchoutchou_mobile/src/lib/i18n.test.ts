import assert from 'node:assert/strict';
import { test } from 'node:test';
import { detectLang, dictionaries, translate } from './i18n.ts';

test('French and English define exactly the same keys', () => {
  assert.deepEqual(Object.keys(dictionaries.fr).sort(), Object.keys(dictionaries.en).sort());
});

test('every {placeholder} in English also appears in French', () => {
  const ph = (s: string) => (s.match(/\{\w+\}/g) ?? []).sort().join(',');
  for (const k of Object.keys(dictionaries.en) as (keyof typeof dictionaries.en)[]) {
    assert.equal(ph(dictionaries.fr[k]), ph(dictionaries.en[k]), `placeholders differ for ${k}`);
  }
});

test('translate substitutes variables', () => {
  assert.equal(translate('en', 'basedOnTrips', { n: 12 }), 'based on 12 trips');
  assert.equal(translate('fr', 'basedOnTrips', { n: 12 }), 'sur 12 trajets');
});

test('detectLang', () => {
  assert.equal(detectLang('fr-FR'), 'fr');
  assert.equal(detectLang('en-GB'), 'en');
  assert.equal(detectLang(undefined), 'en');
});
