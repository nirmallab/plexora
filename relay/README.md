# Plexora relay (`plexora-relay`)

One Cloudflare Worker and one Durable Object class that let the website
(`https://viewer.plexoraapp.com`) reach a remote server with nothing to
download. The contract, written for the browser client and the Python node as
much as for this Worker, is
[docs/internal/WEB_REMOTE_RELAY.md](../docs/internal/WEB_REMOTE_RELAY.md); read
it first. The short version:

| Route | What | Code |
|---|---|---|
| `GET /v1/hello` | `{"relay":1,"ssh_ports":[22],"website":"free"\|"licensed"}`, CORS for `ALLOWED_ORIGINS` | `src/index.ts` |
| `GET /v1/ssh?host=&port=` | WebSocket ↔ TCP, only to sign in and start a node (short-lived) | `src/ssh.ts` |
| `GET /v1/rooms/<room>?role=page\|node` | WebSocket into the room's Durable Object `Room` | `src/room.ts` |

Everything else is 404. The relay never reads a room frame: past the 8-byte
pid, every frame is AES-256-GCM under a key only the page and the node hold.

- **Hibernation.** `Room` accepts every socket with `ctx.acceptWebSocket` (never
  `ws.accept()`), answers `ping` with `pong` through
  `setWebSocketAutoResponse`, and keeps nothing in memory: a socket's role and
  pid are in its tags and attachment, the room's `sha256(roomAuth)` in its
  SQLite storage (`storage.kv`). So sockets stay up while the object is evicted
  (tested locally), and the contract relies on the same for a new deploy (not
  testable locally: check it once after the first real redeploy). Either side
  reconnects on its own if it is dropped anyway. A refusal is an HTTP answer from the object
  that the Worker turns into a socket closed with the code
  (`src/protocol.ts refuseSocket`), so the object accepts only sockets it keeps.
- **Refusals are close codes.** A browser sees no HTTP status on a WebSocket,
  so every refusal is a handshake that closes at once: 4400 bad request, 4401
  wrong room auth, 4403 origin / port / licence, 4404 no such room, 4429 room
  full (8 pages) or rate limited, 4502 TCP connect failed, 4000 replaced by a
  newer node. The handshake always answers `plexora-relay.v1` when it was
  offered, or Chrome drops the connection and the code with it.
- **Rate limits** (`src/ratelimit.ts`, Workers Rate Limiting bindings in
  `wrangler.toml`): per client address (an IPv6 client by its /64), 20
  `/v1/ssh` opens and 30 room creations a minute; past either, 4429 "Too many
  connections from this address; try again in a minute." Joining an existing
  room is not counted. A missing binding, or one that fails, is no limit.
  `wrangler dev` runs the same limits locally.
- **Licence switch** (`src/licence.ts`). `FEATURES_URL` is read at most once a
  minute per isolate; when it cannot be read the last value stands, else
  "free". On "licensed", `/v1/ssh` and page room sockets must offer
  `l.<certificate>`, checked as the website checks it (a port of
  `plexora/client/src/js/services/licenseVerify.js`, the same two platform keys,
  `aud` plexora, not expired after grace). The relay cannot check the
  certificate's device binding or revocation; the page's own check does.

## Tests

```sh
cd relay
npm install
npm test               # all three projects (about 5 s)
npm run test:node      # units: hosts, subprotocols, vars, switch cache, certificates
npm run test:workers   # the Worker and Room in workerd (@cloudflare/vitest-plugin)
npm run test:dev       # end to end through a real `wrangler dev --local`
npm run typecheck
```

- `test:workers` drives the Worker's own fetch handler inside workerd, with
  node-side fixtures from `test/fixtures.ts` (a TCP echo server, an sshd-like
  banner server, a dead port, a features server, certificates signed by a key
  made for the run). It covers forwarding with pids and broadcast, presence,
  replacement, the 8-page cap, eviction (hibernation) and the idle alarm.
- `test:node` runs the certificate check against the `biocognia` package's
  vectors (`BIOCOGNIA_VECTORS`, else `../.venv`'s installed package; skipped,
  with the reason in its name, when neither exists) and holds `PUBLIC_KEYS`
  equal to `licenseVerify.js`'s.
- `test:dev` spawns `wrangler dev --local` on a free port with every var given
  by `--var`, and drives it with Node 22's WebSocket (real Origin header,
  subprotocol answer and close codes). Nothing talks to Cloudflare.

## Local development

```sh
cd relay
cp .dev.vars.example .dev.vars     # localhost origins, private hosts, free switch
npx wrangler dev                   # http://127.0.0.1:8787 (ws://127.0.0.1:8787)
```

Point the browser client at `ws://127.0.0.1:8787` (`/v1/ssh`, `/v1/rooms/…`)
and `http://127.0.0.1:8787/v1/hello`. With the example `.dev.vars`:

- `ALLOWED_ORIGINS="http://localhost:*,http://127.0.0.1:*,https://viewer.plexoraapp.com"`:
  `http://localhost:<any port>` and `http://127.0.0.1:<any port>` are admitted.
  `:*` is accepted on those two loopback hosts only; every other entry is an
  exact origin.
- `ALLOW_PRIVATE_HOSTS="1"`: `/v1/ssh` may open `127.0.0.1`, a VM or a lab
  machine on a private address. Never set it in `wrangler.toml`.
- `SSH_PORTS="22,2222"`, `FEATURES_URL=""` (always free). To try "licensed",
  serve `{"remote_servers":{"website":"licensed","local":"free"}}` from a local
  file server and point `FEATURES_URL` at it; the relay then wants a real
  platform-signed certificate in `l.`.

## Deploy (not done yet; one decision each)

Nothing here has been deployed. When it is time:

1. **Sign in** on the machine that deploys: `npx wrangler login` (the account
   that holds the `plexoraapp.com` zone and the telemetry Worker).
2. **Host.** In `wrangler.toml`, uncomment
   `routes = [{ pattern = "relay.plexoraapp.com", custom_domain = true }]`.
   `wrangler deploy` then creates the DNS record and the certificate itself.
   `workers_dev` stays false, so there is no `*.workers.dev` address.
3. **Vars** (`[vars]` in `wrangler.toml`; no secrets are needed):
   - `ALLOWED_ORIGINS = "https://viewer.plexoraapp.com,https://plexora-viewer-preview.pages.dev"`
     (the website, then its preview build).
   - `SSH_PORTS = "22"`, `ROOM_IDLE_DAYS = "7"`.
   - `FEATURES_URL = "https://api.biocognia.com/v1/features"`.
   - `ALLOW_PRIVATE_HOSTS` must not be set.
4. **Rate limits.** The two `[[ratelimits]]` bindings (`SSH_LIMIT`
   namespace `7301`, `ROOM_CREATE_LIMIT` namespace `7302`) need nothing
   created first; the deploy configures them. A `namespace_id` must not be
   used by another Worker on the account; change these before the first
   deploy if either is.
5. **Durable Objects.** The first deploy applies migration `v1`
   (`new_sqlite_classes = ["Room"]`, SQLite-backed, available on the free
   plan). Never edit a deployed migration; add `v2` for any change.
6. **Deploy:** `cd relay && npm test && npx wrangler deploy`.
7. **Check:** `curl -s -H 'Origin: https://viewer.plexoraapp.com' -i https://relay.plexoraapp.com/v1/hello`
   answers `{"relay":1,"ssh_ports":[22],"website":"free"}` with
   `Access-Control-Allow-Origin: https://viewer.plexoraapp.com`.

After the first redeploy, check that an open room survives it (a page and a
node connected across `wrangler deploy` should see no close, or at worst one
reconnect). `/v1/ssh` sockets may be cut by a deploy; the start flow tolerates
that by design.
