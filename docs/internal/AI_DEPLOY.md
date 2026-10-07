# Plexora AI gateway: where it is deployed now

The gateway used to be part of Plexora's licence Worker (`licensing/`, the
`plexora-licensing` Worker at `license.plexoraapp.com`). That Worker is gone:
licences, devices, credits and every model call now go through the BioCognia
platform, which SCIMAP Pro and Plexora share. Its staging setup, secrets,
seeding, checks and production rollout are documented and run in the
`biocognia-platform` repository (`workers/core` for licences, `workers/ai` for
the gateway), together with the staging and end-to-end tools that used to live
here (`tools/ai_staging.py`, `tools/ai_e2e.py`, and the staging-config test).

The rollout log of the old Worker (staging, the 2026-10-02 production deploy,
schema v4 to v6, the MCP add-on) is in this repository's history up to commit
`f82b3f5c`.

## What a Plexora release does

Plexora owns two inputs the gateway reads, and nothing else:

| Input | Editable source | Uploaded to |
|---|---|---|
| The product manifest (roots, labels, Free tier, AI modules, trial days) | `plexora/licensing/manifest.py` | `PUT {core}/admin/api/products/plexora/manifest` |
| The AI task registry | `plexora/ai/tasks.yaml` | `PUT {ai}/admin/api/ai/registry/plexora` |

```sh
export BIOC_ADMIN_TOKEN=...                       # the platform's admin token
BIOC_CORE_URL=https://<core staging> BIOC_AI_URL=https://<ai staging> \
    python tools/bioc_sync.py --check             # diff both against staging
python tools/bioc_sync.py                          # upload both (staging or production)
python tools/bioc_sync.py --print                  # what would be sent; no network
```

A new task in `tasks.yaml` reaches the gateway with the next upload; until
then the gateway serves it at the product's default (an unknown task falls back
and is recorded), so a Plexora release never waits on a platform deploy.
Plans and prices are the platform's to edit, and only there; none exist for
Plexora yet.

## Checking a build against staging

1. Point a test machine at staging: `BIOCOGNIA_SERVER=<core staging>` and
   `BIOCOGNIA_AI_GATEWAY=<ai staging>`.
2. Settings › License › **Connect this device**, approve the code in the
   staging portal; the status card shows Paid.
3. `plexora ai credits` shows the organisation's balance and the member and
   product lines; an AI gating session on the synthetic project moves the
   meter, and the platform's Usage page shows `plexora.gating.*` calls.
