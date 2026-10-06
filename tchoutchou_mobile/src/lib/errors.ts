import { ApiError } from './api';
import type { Key } from './i18n';

export function errorMessage(t: (k: Key, v?: Record<string, string | number>) => string, e: unknown): string {
  if (e instanceof ApiError) {
    switch (e.kind) {
      case 'network': return t('errServer');
      case 'auth': return t('errAuth');
      case 'rate': return t('errRate');
      case 'sncf': return t('errSncf', { msg: e.message });
      case 'notfound': return t('trainNotFound');
      default: return t('errGeneric', { status: e.status });
    }
  }
  return t('errGeneric', { status: '?' });
}
