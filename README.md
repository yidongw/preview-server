# preview-server

Config and scripts for spinning up per-PR preview environments of the ERP app on a self-hosted Mac server.

Each open PR gets its own isolated build running on a dedicated port, served at `https://erp-pr-<PR>.foxhole.bot` via Caddy.

## How it works

```
GitHub PR event
      │
      ▼
manage-preview.sh start <pr> <branch>
      │
      ├─ cold start (first open)
      │   ├── git worktree add  ~/preview/worktrees/pr-<N>
      │   ├── pnpm install
      │   ├── lingui:compile
      │   ├── pnpm build  (PREVIEW_BUILD=1, production bundle)
      │   ├── pm2 start   (port 4000+N)
      │   ├── warm-up HTTP requests (pre-JIT before user traffic)
      │   └── caddy admin API  →  register route for erp-pr-<N>.foxhole.bot
      │
      └─ hot update (push to existing PR — zero-downtime blue-green swap)
          ├── git fetch + reset --hard
          ├── pnpm install  (only if pnpm-lock.yaml changed)
          ├── lingui:compile  (only if locales/ changed)
          ├── pnpm build        ← old process keeps serving traffic
          ├── pm2 start NEXT    (temp port, found with find_free_port)
          ├── wait + warm-up NEXT
          ├── caddy PUT upstream → NEXT  (atomic, no routing gap)
          ├── pm2 delete OLD
          ├── pm2 start on canonical port, wait + warm-up
          ├── caddy PUT upstream → canonical port
          └── pm2 delete NEXT
```

When a PR is closed or merged:

```
manage-preview.sh stop <pr>
  ├── caddy admin API  →  DELETE route
  ├── pm2 stop + delete  (both canonical and any leftover -next process)
  └── git worktree remove + rm -rf
```

## Prerequisites

- [Caddy](https://caddyserver.com/) with the admin API enabled
- [PM2](https://pm2.keymetrics.io/) (`npm install -g pm2`)
- [pnpm](https://pnpm.io/)
- A clone of the ERP repo at `~/git/carbon`
- A `~/preview/preview.env` file with the app's environment variables (see below)

## Setup

**1. Bootstrap directories**

```bash
mkdir -p ~/preview/{worktrees,logs}
```

**2. Load the initial Caddy config**

```bash
caddy start --config caddy.json
```

Or, if Caddy is already running, reload:

```bash
curl -sf -X POST http://localhost:2019/load \
  -H "Content-Type: application/json" \
  -d @caddy.json
```

**3. Create `~/preview/preview.env`**

Copy from the production `.env` and strip any values that should not be shared with preview builds. The script injects `PORT`, `HOST`, `NODE_ENV`, and `ERP_URL` automatically — do not set those here.

```
DATABASE_URL=...
SESSION_SECRET=...
# etc.
```

## Usage

```bash
# Start (or hot-update) a preview for PR #42 on branch my-feature
./manage-preview.sh start 42 my-feature

# Tear it down
./manage-preview.sh stop 42
```

The preview will be live at `https://erp-pr-42.foxhole.bot` once the port opens.

Logs are written to `~/preview/logs/erp-pr-<N>.log` and `erp-pr-<N>-err.log`.

## Port allocation

| PR number | Port |
|-----------|------|
| 1         | 4001 |
| 42        | 4042 |
| 100       | 4100 |

Formula: `4000 + PR_NUMBER`

## Files

| File | Purpose |
|------|---------|
| `manage-preview.sh` | Main lifecycle script (start / stop) |
| `caddy.json` | Initial Caddy server config loaded at startup |
| `Caddyfile` | Reference Caddyfile (not used directly; caddy.json is authoritative) |

## Blue-green hot update

When a push arrives on an open PR, the old process continues serving user traffic while the new bundle is built. Once the new process is ready and pre-warmed, Caddy's upstream is swapped atomically via a `PUT` to the admin API — there is no window where requests return errors. The script then migrates back to the canonical port so port assignments stay deterministic across restarts.

`find_free_port` scans upward from the candidate port to avoid conflicts if a previous temp process didn't clean up.

## Caveats

- The server must have enough RAM to hold multiple production builds simultaneously. The build step sets `--max-old-space-size=12288` (12 GB).
- During a hot update, two app processes run briefly in parallel (old + new), doubling memory for that PR temporarily.
- Worktrees share the repo's object store but each gets a full `node_modules` install, so disk usage grows with the number of concurrent PRs.
- `preview.env` is gitignored and must be provisioned manually on the host.
