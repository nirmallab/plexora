# Connecting an AI agent to data on another machine

*For anyone whose data sits on an HPC cluster, a lab server or a cloud VM,
and whose AI agent (Claude Code, Codex, Cursor) runs somewhere else, usually
their own laptop.*

On one machine, `plexora ai setup claude|codex|cursor` is all you need. The
agent's client starts Plexora's MCP server itself, over stdio.

When the data is elsewhere, run the server **where the data is**, over HTTP.
The agent then connects to it through an SSH tunnel. Every request carries a
token, and the token's scope limits what that agent may do.

## On the machine with the data

```bash
# 1. Make a token. It is printed once and never stored, so copy it now.
plexora ai token create --scope read --label "my laptop"

# 2. Start the server. It listens on 127.0.0.1:8321, which only this
#    machine can reach, and it refuses to start if no token exists.
plexora mcp serve --transport http
```

The token scopes are:

| Scope   | The agent may                                                              |
|---------|----------------------------------------------------------------------------|
| `read`  | look: inspect projects, render regions, and count and summarise cells      |
| `write` | also change Plexora's own state: gates and regions, all receipted and undoable |
| `admin` | whatever the server was started to allow, such as `--allow-source-writes`  |

A token only ever narrows what the server allows. An `admin` token on a
server started without `--allow-source-writes` still cannot write your files.

To see your tokens, or to stop accepting one:

```bash
plexora ai token list
plexora ai token revoke <id>
```

## On your laptop

```bash
# 1. The tunnel. Leave it running.
ssh -N -L 8321:127.0.0.1:8321 you@the-data-host

# 2. Register the server with your client. This writes the client's config,
#    which names the token variable, never the token itself.
plexora ai setup claude --http http://127.0.0.1:8321/mcp

# 3. Give the client the token, in the shell it starts from.
export PLEXORA_MCP_TOKEN=plx_…
```

Restart the client, then ask it: *"What Plexora projects do I have?"*

On an HPC cluster, run the server on the compute node, as you would Plexora
itself. Then tunnel through the login node:
`ssh -N -L 8321:<node>:8321 you@login-host`.

## Checks when it does not connect

- `curl http://127.0.0.1:8321/health` from the laptop, with the tunnel up,
  should answer `{"ok": true, ...}`. It needs no token.
- A **401** means the token is missing, mistyped, revoked or expired. Check
  `echo $PLEXORA_MCP_TOKEN` in the shell the client started from.
- A **421 or 403** behind a reverse proxy means the proxy sends its own host
  name. Add it with `--allowed-host proxy.example.org`.
- `plexora ai audit` on the data host lists every change an agent made or
  attempted, and which token made it.

## Without a token

`--no-auth` turns token checks off, and is refused unless the host is
127.0.0.1. Use it only on a machine where nothing else you do not trust runs,
since any process on that machine could then call the server.
