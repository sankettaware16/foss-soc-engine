import type { AppMountParameters, CoreSetup, CoreStart, Plugin } from '@kbn/core/public';
import { PLUGIN_ID, PLUGIN_NAME } from '../common';
import type { TlsocParserPluginSetup, TlsocParserPluginStart } from './types';

export class TlsocParserPlugin
  implements Plugin<TlsocParserPluginSetup, TlsocParserPluginStart>
{
  public setup(core: CoreSetup): TlsocParserPluginSetup {
    core.application.register({
      id: PLUGIN_ID,
      title: PLUGIN_NAME,
      order: 9000,
      // Its own nav category so it sits in a clear "TLSOC" section.
      category: {
        id: 'tlsoc',
        label: 'TLSOC',
        order: 6000,
        euiIconType: 'inspect',
      },
      async mount(params: AppMountParameters) {
        const [coreStart] = await core.getStartServices();
        const { renderApp } = await import('./application');
        return renderApp(coreStart, params);
      },
    });
    return {};
  }

  public start(_core: CoreStart): TlsocParserPluginStart {
    return {};
  }

  public stop() {}
}
