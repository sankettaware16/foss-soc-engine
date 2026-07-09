# TLSOC Parser — build & install into your ELK stack

Two things get deployed:

1. **`tlsoc-parser-ui`** — a container running the engine's Web UI headless (the
   plugin's backend). Easy: one `docker build`.
2. **`tlsocParser`** — the Kibana plugin itself. This must be **built inside a
   Kibana 8.19.12 source tree** (the plugin toolchain is version-locked — there
   is no supported out-of-tree build for 8.19). Below is the exact, verified
   procedure.

> Versions confirmed from the Kibana `8.19` branch: Node **22.22.2** (`.nvmrc`),
> manifest is a flat **`kibana.json`**, build output is
> `build/tlsocParser-8.19.12.zip`, plugins load from
> `/usr/share/kibana/plugins/<id>/`.

---

## Part 1 — Build the backend image

From the **engine repo root** (the folder containing `webui/`, `core/`, …):

```bash
docker build -f elk-plugin/backend/Dockerfile -t tlsoc-parser-ui:1.0.0 .
```

(The repo-root `.dockerignore` keeps the multi-GB sample logs out of the build.)

---

## Part 2 — Build the Kibana plugin

You need a machine with **git**, **Node 22.22.2** (via `nvm`), and **yarn**.

```bash
# 1. Kibana source at the EXACT tag
git clone https://github.com/elastic/kibana.git
cd kibana
git checkout v8.19.12
nvm install    # reads .nvmrc -> 22.22.2   (or: nvm install 22.22.2 && nvm use)

# 2. Bootstrap (installs deps, builds packages — takes a while, needs a few GB)
yarn kbn bootstrap

# 3. Put THIS plugin under Kibana's plugins/ dir (git-ignored by Kibana).
#    Copy elk-plugin/kibana-plugin/ from the engine repo to plugins/tlsocParser
mkdir -p plugins
cp -r /path/to/foss-soc-engine/elk-plugin/kibana-plugin plugins/tlsocParser

# 4. Build the distributable, pinned to the target Kibana version
cd plugins/tlsocParser
yarn build --kibana-version 8.19.12
#    -> creates:  plugins/tlsocParser/build/tlsocParser-8.19.12.zip
#       (add --skip-archive to get an unzipped build/kibana/tlsocParser/ folder instead)
```

If the build complains it can't find the plugin, confirm it sits **inside**
`kibana/plugins/` (the `package.json`/`tsconfig.json` use `../../` paths that
resolve to the Kibana root).

---

## Part 3 — Install into your running stack

Your `docker-compose.yml` already mounts
`./kibana/installed_plugins/<name> → /usr/share/kibana/plugins/<name>` (that's
how `nlToKql` is installed). Do the same:

```bash
cd /opt/TLSOCDockerDeploy

# Unzip the built plugin into the installed_plugins mount:
mkdir -p kibana/installed_plugins
unzip /path/to/kibana/plugins/tlsocParser/build/tlsocParser-8.19.12.zip \
      -d /tmp/tlsocParser-build
#   the zip contains  kibana/tlsocParser/…  -> take that inner folder:
cp -r /tmp/tlsocParser-build/kibana/tlsocParser kibana/installed_plugins/tlsocParser
```

Then edit `docker-compose.yml` (see `elk-plugin/deploy/docker-compose.snippet.yml`):

1. **Add the backend service** `tlsoc-parser-ui` (copy the block from the
   snippet; fix the `context:` path + the engine `volumes:` host paths).
2. **Add to your existing `kibana:` service:**
   ```yaml
   kibana:
     volumes:
       # ...existing mounts...
       - ./kibana/installed_plugins/tlsocParser:/usr/share/kibana/plugins/tlsocParser
     environment:
       # ...existing env...
       - TLSOCPARSER_BACKENDURL=http://tlsoc-parser-ui:8600
     depends_on:
       - tlsoc-parser-ui
   ```
   (`TLSOCPARSER_BACKENDURL` maps to the plugin's `tlsocParser.backendUrl`
   config; you could instead put `tlsocParser.backendUrl: http://tlsoc-parser-ui:8600`
   in a mounted `kibana.yml`.)

3. **Bring it up:**
   ```bash
   docker compose up -d --build tlsoc-parser-ui
   docker compose up -d kibana          # recreates Kibana with the plugin + env
   docker compose restart kibana        # if it was already running
   ```

Kibana validates plugin version on boot — this is why you built with
`--kibana-version 8.19.12`. Watch it load:
```bash
docker compose logs -f kibana | grep -i -E "tlsocParser|plugin"
```

---

## Part 4 — Use it

Open Kibana → left nav → **TLSOC** section → **TLSOC Parser**. You get the Test
Log / Rules / Config / ECS Helper / Monitor tabs, authenticated by your Kibana
login (no separate password). Because the backend is bind-mounted to the real
engine dir, edits and tests act on production rules/config and the Monitor shows
the live engine.

### Verify the wiring
- **Backend reachable:** `docker compose exec kibana curl -s http://tlsoc-parser-ui:8600/api/whoami` → JSON.
- **Proxy working:** in the TLSOC Parser UI, the Rules tab should list your rules.
- **Monitor:** shows 🟢 running when the engine (systemd `foss-soc`) is processing
  logs (it reads the bind-mounted `logs/`). If it shows 🔴 but the engine is up,
  the `logs/` mount path is wrong — fix `SOC_LOG_DIR` / the volume.

### Trust boundary (security — read once)

The `tlsoc-parser-ui` backend runs **without its own login**
(`SOC_UI_NO_AUTH=1`) because **Kibana is the authentication layer**: users
reach it only through the plugin's server-side proxy, which sits behind the
Kibana login. That is safe under exactly one condition: **port 8600 must stay
Docker-network-internal**. The shipped compose snippet deliberately has no
`ports:` mapping for it — never add one, never attach the service to a
shared/external network, and never run the same container on a host network.
Anyone who can reach 8600 directly can edit your production rules and config
without any credentials.

---

## Troubleshooting

- **Plugin doesn't appear in the nav** → version mismatch (rebuild with
  `--kibana-version 8.19.12`), or it's in `external_plugins/` (a staging mount
  Kibana does NOT auto-load) instead of `plugins/`. It must land at
  `/usr/share/kibana/plugins/tlsocParser/`.
- **"Cannot reach the parser backend" (502)** → `tlsoc-parser-ui` isn't up or
  `TLSOCPARSER_BACKENDURL` is wrong. Check `docker compose ps` and the service name.
- **Rules save but the engine ignores them** → the backend container isn't
  mounted to the real engine `rules/`. Fix the `volumes:` host path.
- **Build fails out-of-tree** → the 8.19 plugin helpers require a Kibana source
  checkout; build inside `kibana/plugins/tlsocParser` as in Part 2.

---

## Optional polish
- **Dark-mode theming:** the scaffold wraps the app in `I18nProvider` only. To
  match Kibana's dark/light theme exactly, also wrap in `KibanaThemeProvider`
  (from `@kbn/react-kibana-context-theme`) fed `theme$` from `AppMountParameters`
  in `public/application.tsx`.
- **Feature privilege:** the proxy routes opt out of authorization
  (`security.authz.enabled: false`) — any logged-in Kibana user can use them. To
  gate it behind a specific privilege, register a Kibana feature and switch to
  `security: { authz: { requiredPrivileges: [...] } }` in `server/plugin.ts`.
