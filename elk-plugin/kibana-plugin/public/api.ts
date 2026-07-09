import type { HttpStart } from '@kbn/core/public';

// All calls go through the plugin's own server proxy, which forwards them to the
// Python engine's /api/<path>. Kibana adds the base path + CSRF header for us.
const BASE = '/api/tlsoc_parser';

export class ParserApi {
  constructor(private readonly http: HttpStart) {}

  get<T = any>(path: string, query?: Record<string, any>): Promise<T> {
    return this.http.get<T>(`${BASE}${path}`, query ? { query } : undefined);
  }

  post<T = any>(path: string, body?: any): Promise<T> {
    return this.http.post<T>(`${BASE}${path}`, {
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  }
}
