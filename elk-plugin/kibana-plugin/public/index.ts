import { TlsocParserPlugin } from './plugin';

export function plugin() {
  return new TlsocParserPlugin();
}

export type { TlsocParserPluginSetup, TlsocParserPluginStart } from './types';
