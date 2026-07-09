import { firstValueFrom } from 'rxjs';
import { schema } from '@kbn/config-schema';
import type {
  CoreSetup,
  CoreStart,
  Plugin,
  PluginInitializerContext,
  Logger,
  RequestHandler,
} from '@kbn/core/server';
import type { TlsocParserConfig } from './index';
import type { TlsocParserPluginSetup, TlsocParserPluginStart } from './types';

// The wildcard "{path*}" captures the whole remaining sub-path (incl. slashes)
// into request.params.path. We forward it to the Python engine's /api/<path>.
const proxyValidation = {
  params: schema.object({ path: schema.string({ defaultValue: '' }) }),
  query: schema.recordOf(schema.string(), schema.any()),
};
const proxyValidationWithBody = {
  ...proxyValidation,
  body: schema.maybe(schema.any()),
};

// SSRF guard: the target host is a FIXED config value; only the sub-path is
// user-controlled, so strip any '.'/'..' segments before appending it.
function safeSubPath(p: string): string {
  return (p || '')
    .split('/')
    .filter((seg) => seg && seg !== '.' && seg !== '..')
    .join('/');
}

function makeProxyHandler(
  getBackendUrl: () => Promise<string>,
  logger: Logger
): RequestHandler<{ path: string }, Record<string, unknown>, unknown> {
  return async (_context, request, response) => {
    const base = (await getBackendUrl()).replace(/\/+$/, '');
    const subPath = safeSubPath(request.params.path);

    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(request.query ?? {})) {
      if (Array.isArray(v)) v.forEach((item) => qs.append(k, String(item)));
      else if (v != null) qs.append(k, String(v));
    }
    const query = qs.toString();
    const targetUrl = `${base}/api/${subPath}${query ? `?${query}` : ''}`;

    const method = request.route.method.toUpperCase();
    const hasBody = method !== 'GET' && method !== 'HEAD' && request.body != null;

    try {
      // Node 22 (Kibana 8.19) has global fetch.
      const upstream = await fetch(targetUrl, {
        method,
        headers: { 'content-type': 'application/json', accept: 'application/json' },
        body: hasBody ? JSON.stringify(request.body) : undefined,
        signal: AbortSignal.timeout(30_000),
      });

      const ct = upstream.headers.get('content-type') ?? '';
      const isJson = ct.includes('application/json');
      const payload = isJson ? await upstream.json() : await upstream.text();

      if (!upstream.ok) {
        return response.customError({
          statusCode: upstream.status,
          body:
            typeof payload === 'string'
              ? { message: payload }
              : (payload as Record<string, unknown>),
        });
      }
      return response.ok({ body: payload as Record<string, unknown> });
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      logger.error(`tlsocParser proxy to ${targetUrl} failed: ${msg}`);
      return response.customError({
        statusCode: 502,
        body: { message: `Cannot reach the parser backend: ${msg}` },
      });
    }
  };
}

export class TlsocParserPlugin
  implements Plugin<TlsocParserPluginSetup, TlsocParserPluginStart>
{
  private readonly logger: Logger;
  private readonly config$;

  constructor(initContext: PluginInitializerContext) {
    this.logger = initContext.logger.get();
    this.config$ = initContext.config.create<TlsocParserConfig>();
  }

  public setup(core: CoreSetup): TlsocParserPluginSetup {
    const router = core.http.createRouter();
    const getBackendUrl = async () => (await firstValueFrom(this.config$)).backendUrl;

    // The proxy only needs an authenticated Kibana session, not a specific
    // privilege, so it opts out of route authorization (8.16+ requirement).
    const security = {
      authz: {
        enabled: false as const,
        reason:
          'Thin proxy to an internal parser microservice; requires an authenticated ' +
          'Kibana session only, not a specific privilege.',
      },
    };
    const handler = makeProxyHandler(getBackendUrl, this.logger);

    router.get({ path: '/api/tlsoc_parser/{path*}', validate: proxyValidation, security }, handler);
    router.post({ path: '/api/tlsoc_parser/{path*}', validate: proxyValidationWithBody, security }, handler);
    router.put({ path: '/api/tlsoc_parser/{path*}', validate: proxyValidationWithBody, security }, handler);
    router.delete({ path: '/api/tlsoc_parser/{path*}', validate: proxyValidationWithBody, security }, handler);

    this.logger.info('tlsocParser: proxy routes registered');
    return {};
  }

  public start(_core: CoreStart): TlsocParserPluginStart {
    return {};
  }

  public stop() {}
}
