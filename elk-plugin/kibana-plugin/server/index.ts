import { schema, type TypeOf } from '@kbn/config-schema';
import type { PluginConfigDescriptor, PluginInitializerContext } from '@kbn/core/server';

// ---- Plugin config: where the Python engine backend lives. --------------
// Set in kibana.yml as:   tlsocParser.backendUrl: "http://tlsoc-parser-ui:8600"
// or via env:             TLSOCPARSER_BACKENDURL=http://tlsoc-parser-ui:8600
export const configSchema = schema.object({
  backendUrl: schema.string({ defaultValue: 'http://tlsoc-parser-ui:8600' }),
});
export type TlsocParserConfig = TypeOf<typeof configSchema>;

export const config: PluginConfigDescriptor<TlsocParserConfig> = {
  schema: configSchema,
};

export async function plugin(initializerContext: PluginInitializerContext) {
  const { TlsocParserPlugin } = await import('./plugin');
  return new TlsocParserPlugin(initializerContext);
}

export type { TlsocParserPluginSetup, TlsocParserPluginStart } from './types';
