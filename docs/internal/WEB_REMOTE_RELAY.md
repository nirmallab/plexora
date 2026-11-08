# Remote servers from the website: the relay

Status: being built (2026-11-08). Decided by the user on 2026-11-08:

- The website connects to SSH servers with **no download**.
- A connection must never be **dropped** by the relay. A Cloudflare restart may cost a second, but never a session or a login.

This reverses BROWSER_NATIVE_INVESTIGATION.md's "no relay" decision (2026-11-05) for this one feature.

## Why this shape

A browser can open neither SSH nor raw TCP. A plain Worker holding an SSH connection is cut at Cloudflare's runtime updates: existing WebSockets get up to 20 minutes, then are disconnected (developers.cloudflare.com, Workers limits, "Runtime updates"). So SSH is used only to **start** the node, which takes seconds. The node's lifetime is never tied to any one connection:

1. **SSH start.** The page opens SSH through `GET /v1/ssh` (WebSocket ↔ TCP), signs in (the password and Duo are typed in the page), and runs a start script. The script writes the relay file, starts the node *detached* (`nohup setsid [srun …] plexora node serve … --relay-file F &`), and tails its log until the node joins its room. Then the page closes SSH.
   - On O2 (2026-11-08), compute nodes reach the internet over HTTPS (wss echo from compute-b-16-186 worked) and the login node's logind has KillUserProcesses=no.
2. **Room.** The node dials *out* to `wss://relay…/v1/rooms/<room>` and the page dials the same room. A Durable Object (DO) forwards bytes between them. Its WebSockets are accepted with the **Hibernation API**, so they stay connected through code updates and eviction. Either side may still be disconnected (a forced update, a runtime restart, a network blip). Both reconnect on their own, the node keeps running, and unfinished requests are sent again (see Retries).
3. **End-to-end encryption.** Every frame is AES-256-GCM under a key the page made and handed to the node over SSH. The relay sees sizes, timing and the room id, never content.

## Relay Worker (`relay/`, Worker `plexora-relay`, host `relay.plexoraapp.com`)

Configuration (wrangler `[vars]`, strings):

- `ALLOWED_ORIGINS`: comma-separated page origins, default `https://viewer.plexoraapp.com`.
- `SSH_PORTS`: comma-separated TCP ports `/v1/ssh` may open, default `22`.
- `ROOM_IDLE_DAYS`: how long a room with no sockets keeps its record, default `7`.
- `FEATURES_URL`: where the free/licensed switch is read (see Licence switch). Empty means always free.

Endpoints:

- `GET /v1/hello` returns `{"relay":1,"ssh_ports":[22],"website":"free"|"licensed"}`. CORS is allowed for `ALLOWED_ORIGINS`.
- `GET /v1/ssh?host=<dns name or public IP>&port=<n>` is a WebSocket upgrade.
  - `Origin` must be in `ALLOWED_ORIGINS` (browser only).
  - `port` must be in `SSH_PORTS`.
  - `host` must be a DNS name or an IP literal outside private, loopback, link-local and CGNAT ranges.
  - Subprotocols: the page offers `plexora-relay.v1` (answered; without an answerable protocol Chrome fails the handshake and the close code is lost) and, when the website switch is "licensed", `l.<certificate>`.
  - Binary messages are TCP bytes in both directions. Text frames are ignored.
  - Close codes:
    - 4400 for a bad request, including a private, reserved or malformed host (also `localhost`, number-like names, and IPv4 embedded in IPv6);
    - 4403 for a refused origin, licence or port;
    - 4429 when one address opens too many;
    - 4502 when the TCP connect failed or took over 15 s (reason = the error);
    - 1000 when the server hung up, 1011 for a TCP error mid-stream. A plain HTTP request gets 426.
  - Every refusal is an accepted handshake closed at once with its code, so the client can read why.
  - The plain Worker holds this connection. It is short-lived by design.
- `GET /v1/rooms/<room>?role=page|node` is a WebSocket upgrade handled by the DO `Room` (`idFromName(room)`).
  - `room` matches `[A-Za-z0-9_-]{22,64}`.
  - Subprotocols (`Sec-WebSocket-Protocol`):
    - `plexora-relay.v1`: required. The server answers with exactly this one.
    - `a.<roomAuth>`: required. `roomAuth` is 43 chars of base64url (32 random bytes).
    - `l.<licence certificate>`: optional, read only when the website switch is "licensed" and the role is page.
  - **Room creation.** The first *page* that presents an auth creates the room: the DO stores `sha256(roomAuth)` in its SQLite storage. A *node* can only join an existing room, otherwise 4404. A later socket whose auth hash differs gets 4401. Comparisons are constant-time.
  - **Page sockets.** `Origin` must be in `ALLOWED_ORIGINS`. **Node sockets** send no Origin and are not checked for one.
  - **Accept.** Sockets are accepted with `ctx.acceptWebSocket(ws, tags)`:
    - A page is tagged `["page", "page:<pid>"]`, where `pid` is 8 random bytes in hex. The tag and `serializeAttachment({role, pid})` both carry it.
    - A node is tagged `["node"]`.
    - At most one node: a new node socket closes the previous one with 4000 "replaced".
    - At most 8 pages: the ninth gets 4429.
  - **Ping.** `setWebSocketAutoResponse("ping" → "pong")`. Both sides send the text `ping` every 30 s and treat 75 s without a pong as a dead socket.
  - **Presence.** After every accept and every close, the DO sends the text frame `{"t":"presence","node":true|false,"pages":<n>}` to every socket.
  - **Forwarding** (binary frames only; all other text frames are ignored):
    - page → DO: `payload`. DO → node: `pid(8 bytes) ‖ payload`.
    - node → DO: `pid(8 bytes) ‖ payload`. DO → the page tagged `page:<pid>`, without the pid. A frame for a pid that is not there is dropped. An all-zero pid is sent to every page.
  - **Expiry.** Each accept sets an alarm `ROOM_IDLE_DAYS` ahead. When it fires with no sockets attached, the DO deletes its storage.
  - Message limit: 32 MiB in (Cloudflare's). Senders keep frames ≤ 1 MiB + 64 bytes.

## Frames (end to end; `plexora/relay_wire.py` is the reference, with the vectors in `tests/fixtures/relay_wire_vectors.json`)

**Key.** `K` is 32 random bytes, sent to the node in the relay file as base64url.

**Sealed frame.** `nonce(12) ‖ AES-256-GCM(K, nonce, plaintext, aad)`, where:

- the nonce is random per frame;
- `aad = "plexora-relay.v1|" + room + "|" + dir`;
- `dir` is `p2n` for page → node and `n2p` for node → page. A frame replayed in the other direction therefore fails.

**Plaintext.** `kind(u8) ‖ stream(u32 BE) ‖ hlen(u32 BE) ‖ header(JSON utf-8, hlen bytes) ‖ body`.

| kind | name | dir | header | body |
|---|---|---|---|---|
| 1 | HELLO | both | page: `{"client":"<id>","v":1}`. node: `{"v":1,"node_id","api_version","token","version","hostname","platform"}` | – |
| 2 | REQUEST | p2n | `{"m":method,"p":path+query,"h":[[k,v],…],"client":"<id>","end":bool}` | first body chunk |
| 3 | REQUEST_BODY | p2n | `{"end":bool}` | chunk |
| 4 | RESPONSE_HEAD | n2p | `{"s":status,"h":[[k,v],…]}` | – |
| 5 | RESPONSE_BODY | n2p | `{"end":bool}` or `{"end":true,"error":"…"}` | chunk ≤ 1 MiB |
| 6 | CANCEL | p2n | `{}` | – |
| 7 | STOP | p2n | `{}`: the node shuts down; its job ends | – |

**Streams.**

- Stream ids are chosen by the page client: a random u32 start, +1 per request, never 0. Stream 0 is HELLO and STOP.
- Each page connection sends HELLO first. The node replies with its HELLO addressed to that pid.
- On joining, the node also sends HELLO to the all-zero pid.

**On the node.**

- A REQUEST is sent to its own HTTP server at `127.0.0.1:<port>`. `X-Plexora-Node-Token` is set to the node's token when the request lacks it.
- Hop-by-hop headers (`connection`, `keep-alive`, `transfer-encoding`, `upgrade`, `host`, `content-length`) and `accept-encoding` are dropped: the page gets bytes as served, unencoded.
- The node remembers which pid each client id last spoke from, and addresses responses to the client's *current* pid.

**Retries.**

- The page client keeps a request pending until its RESPONSE_BODY `end`, and hands its caller a whole body. The website's service-worker relay moves whole bodies anyway.
- When the page's socket drops, or presence says `node:false`, every pending request is cleared of what it had received. Each is sent again with the **same** stream id once both sides are back.
- The node keeps the complete responses to non-GET/HEAD requests for 120 s (64 MiB in total, oldest dropped first), keyed by `(client, stream)`.
  - A REQUEST for a key it is still answering is not executed twice: the output goes to the client's current pid when it is ready.
  - A REQUEST for a key it has kept is answered from that copy.
  - A GET or HEAD is simply executed again.
- A node HELLO (to this pid or to all) means the node may have lost what it had in hand: its old socket can be replaced without the page ever seeing `node:false`. So the page sends again everything that has not finished.
- A request may take 600 s without a byte before the page fails it. The wire has no keepalive.

## Relay file and `plexora node serve --relay-file F`

`F` is JSON: `{"v":1,"relay":"wss://relay.plexoraapp.com","room":"…","auth":"…","key":"<b64url>","idle_exit":1800}`. It is written mode 0600 by the start script.

When the node starts:

- It reads F, then deletes it.
- Once its HTTP server is bound, it starts the relay link: one daemon thread running an asyncio loop with aiohttp's WebSocket client.
- It prints `[plexora-relay] joined <room>` to stdout each time it joins (the start script's tail waits for the first).
- Reconnects use 1, 2, 4… seconds up to 30, with jitter, for as long as the node runs.

The node exits:

- on STOP;
- when no page has been present for `idle_exit` seconds (0 = never);
- at the job's walltime;
- or when it is signalled.

## Start script (built by the website's Python, run by the page over SSH)

`sh -c '<script>'` with the relay file as one line on stdin:

```
umask 077; mkdir -p ~/.plexora/relay
IFS= read -r line; printf '%s\n' "$line" > ~/.plexora/relay/<room>.json
[install line, as `connect.install_prefixed`, when the profile asks]
nohup setsid <srun line | > <node command line> --relay-file ~/.plexora/relay/<room>.json \
    > ~/.plexora/relay/<room>.log 2>&1 < /dev/null &
tail -n +1 -F ~/.plexora/relay/<room>.log
```

The page reads the tail's lines as the desktop reads ssh's: the queue, the announce, failures. It closes SSH once the node has joined the room.

## Files-only servers (`Remote.files`: SFTP, nothing of ours runs there)

HMS O2's transfer node, or any SFTP server: no node can be started, so nothing
over there can resume a connection, and the page's SSH connection *is* the
connection (services/webRemote.js `runFiles`).

- **Plan.** `web_remote.files_plan(remote)` → `{kind: "files", user, host, port,
  target, timeout}`; no command. The port is the address's (`host:2222`), then a
  `Port=` among the profile's `ssh_opts`, then 22.
- **Page.** Signs in as for a node (`signIn`: host key, questions to the dialog),
  then opens the `sftp` subsystem on a channel (ssh/sshEntry.js `Sftp`: SFTP v3
  as `server/utils/sftp.py` speaks it: 64 requests in flight, reads of up to
  256 KiB per `limits@openssh.com`, 32 cached handles, links followed in a
  listing) and records `home` (`realpath(".")`). SSH keep-alive every 30 s.
- **Python's reads.** `POST /__plexora/remote/v1/files/<profile name>` with
  `{op: stat|lstat|realpath|listdir|read, path, start, end}`: JSON, or the bytes
  for `read`. Errors: 404/403/500 `{error, fx}` (SFTP status), 503 `{error,
  not_connected: true}`. By name, so a connection signed in again answers with
  nothing re-registered. `web_remote.PageFiles` is `sftp`'s client for it,
  registered under the canonical target while the page's view carries
  `files.home` and the session has not ended; `sftp.client_for` asks
  `web_remote.files_client` (set by browser/boot.py) when nothing is
  registered. `PageFiles.run` runs in place (no threads in Pyodide).
- **Drops.** SSH closing under a connected session (relay restart, network,
  the SFTP side ending) → state `connecting` ("signing in again"), the window
  event `plexora-remote-attention` (+ `takeAttention()`), which the app frame's
  connectionModal.js turns into a background watch: the dialog comes back for
  the password/Duo, and a notice says when it is connected. Transport failures
  while signing in again are retried 6 times, 5 s × attempt apart, before
  failing. A second drop within 60 s of signing in again ends the connection
  (`exited`). Reads made meanwhile wait up to 300 s (`FILES_WAIT_MS`); a read
  the drop cut off is asked again once (every op only reads).
- **Reload.** The tab's files connections are kept in `sessionStorage`
  (`plexora.webRemote.files`, plan + home; never a secret) and signed in to
  again from `configure()`; the dialog is brought back the same way.
- **Limits.** Every drop or reload costs a sign-in (a Duo push on O2): SSH
  cannot resume, and nothing of ours runs over there to resume against. Where
  the files are also reachable from a machine that can run a node (O2's
  transfer node shares /n/ with the login nodes), the node path has neither.

## Licence switch

Two global switches sit in the core admin (admin.biocognia.com/admin/sales#features, "Plexora features" card), stored in the platform `settings` table:

- `REMOTE_SERVERS_WEBSITE` and `REMOTE_SERVERS_LOCAL`, each `free` (the default) or `licensed`.
- Served publicly at `GET https://api.biocognia.com/v1/features` as `{"remote_servers":{"website":"free","local":"free"}}`, with open CORS and `Cache-Control: max-age=60`.

Where each is enforced:

- **Website.** The relay checks `/v1/ssh` and page room sockets: when the switch is "licensed", the `l.` certificate must verify. The page's Python refuses earlier with the standard licence wording.
- **Local sessions** (desktop, pip `plexora`). `POST /settings/remotes/<name>/connect` refuses when the switch is "licensed" and the install holds no valid licence. The value is cached for an hour; when it cannot be read, the last known value is used, else free.
