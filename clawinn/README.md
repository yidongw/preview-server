# ClawInn Linux Docker previews

This is a separate manager for Oracle Linux hosts. The repository's Carbon/Mac scripts are unchanged. Install this manager from a reviewed, pushed preview-server commit; never run the manager from a PR checkout. Application builds use only the exact pushed TokenToEarn PR head.

## Request interface

A trusted workflow sends one JSON object on stdin to a fixed SSH forced command:

```sh
python3 -I /opt/preview-server/clawinn/preview.py --config /etc/clawinn-preview/config.json request
```

```json
{"repository":"cyl19970726/TokenToEarn","prNumber":42,"sha":"0123456789abcdef0123456789abcdef01234567","action":"deploy","labels":[]}
```

The only actions are `deploy`, `stop`, `reset-db`, and `refresh-dev-copy`. Unknown fields, other repositories, invalid SHAs/PR numbers and invalid action/mode combinations fail. Conflicting database labels fail except on `stop`, which must remain able to clean up a closed PR. Branches, paths, shell commands, environment variables and credentials cannot be passed in a request. The trusted GitHub workflow must check collaborator permissions before selecting a shared mode; possession of this SSH key grants preview deployment authority. Do not give that key to PR jobs or fork workflows.

The manager independently reads GitHub's current PR: repository, open state, same-repository head, main base, SHA and database mode must match. It fetches `refs/pull/N/head` into a manager-owned bare clone and checks that commit too. These checks repeat after building and immediately before route publication. No extra opt-in label is required; workflow configuration controls whether previews are enabled. For `stop`, a closed PR is accepted; an old event for a different open SHA cannot stop a newer preview. GitHub authentication/rate-limit failures fail closed.

Success writes exactly one JSON object to stdout. For example:

```json
{"state":"ready","prNumber":42,"sha":"0123456789abcdef0123456789abcdef01234567","databaseMode":"isolated","storefrontUrl":"https://pr-42.preview.clawinn.xyz","adminUrl":"https://pr-42.preview-admin.clawinn.xyz"}
```

Additional fields include image, loopback port and timestamp. Failure exits 1 and prints a sanitized message to stderr; raw Docker/build/helper output is never forwarded. Each lifecycle stage is fsynced into the private `journal.jsonl` and atomically replaced `pr-N/receipt.json`. The receipt is diagnostic state; `ready` is set only after both public health endpoints return the requested app SHA and Caddy route SHA.

## Host preparation

Use a dedicated Linux VM for reviewed same-repository PRs. Never install/build on cx43/control hosts. Docker's socket is root-equivalent: the SSH service account and the installed manager/config must be trusted, and PR containers must never receive the socket. Containers are not a security boundary for arbitrary hostile code; do not expand this setup to unreviewed fork PRs.

Install Python 3.9+ (stdlib only), Git, Docker Engine 25+, the buildx plugin, and Caddy. Configure wildcard DNS and HTTPS certificates for `*.preview.clawinn.xyz` and `*.preview-admin.clawinn.xyz`. Wildcard certificate issuance needs an operator-managed DNS challenge or equivalent certificate provisioning. Restrict ports 2019 and 1234 to loopback. Only public 80/443 and restricted SSH should be exposed.

Use a reviewed bare clone with its origin fixed to `https://github.com/cyl19970726/TokenToEarn.git`. The manager uses only Git archive; it never checks out/extracts the tar on the host, runs host Node/npm, loads PR Python, or copies source into running containers. Keep Git and Docker updated. A malicious Dockerfile may request network access during dependency builds, so isolate this VM's egress from private/control/production networks at the host or cloud firewall.

Create `/srv/clawinn-preview` root-owned mode 0700, `/etc/clawinn-preview` mode 0700, and a manager-specific home `/srv/clawinn-preview/home` mode 0700. Config, optional token file, installed code, bare clone and baseline must be writable only by the operator. Runtime data directories are owned by UID/GID 1000 by default (the Dockerfile's `node` user). The manager must run as root to assign that ownership, or as the configured UID with access to Docker; do not share that account with unrelated workloads.

### Bounded rootless BuildKit

Do not use buildx's default privileged `docker-container` builder. Provision a dedicated **rootless, nonprivileged** BuildKit container and a buildx `remote` driver to it. Choose and pin a reviewed `moby/buildkit:rootless` digest, verify the host supports rootless user namespaces, and use its documented AppArmor/seccomp settings. A representative operator command is:

```sh
docker run -d --name clawinn-preview-buildkit --restart unless-stopped \
  --user 1000:1000 --memory 4g --memory-swap 4g --cpu-period 100000 --cpu-quota 200000 \
  --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
  --pids-limit 512 --log-driver local --log-opt max-size=5m --log-opt max-file=2 \
  --mount type=volume,source=clawinn-preview-buildkit,target=/home/user/.local/share/buildkit \
  --publish 127.0.0.1:1234:1234 \
  moby/buildkit:rootless@sha256:REPLACE_WITH_REVIEWED_DIGEST \
  --addr tcp://0.0.0.0:1234 --oci-worker-no-process-sandbox
HOME=/srv/clawinn-preview/home docker buildx create --name clawinn-preview \
  --driver remote tcp://127.0.0.1:1234
```

The manager validates the builder's remote endpoint and the named BuildKit container's non-root user, absence of `Privileged`, memory limit and CPU quota before every build. Rootless BuildKit's `no-process-sandbox` limitation requires a dedicated disposable build container; never place secrets or host mounts inside it. The BuildKit listener has no authentication and must remain loopback-only. The builder config is in this dedicated HOME, not the SSH login's default Docker config.

Put Docker/BuildKit data on a dedicated filesystem with an operator-enforced disk quota and configure BuildKit GC/retention. `minFreeBytes` is a preflight floor, not a disk quota; Docker cache, application images, private backups and reset archives require operator retention. Do not prune image versions referenced by `slots/*/image.json`: the previous helper image is needed for consistent backups. Do not perform blanket Docker prune on a shared host. The manager refuses low free space, but cannot prevent one build exhausting an unbounded filesystem. Never remove guards or fall back to a privileged builder if setup fails.

### Config

Install `/etc/clawinn-preview/config.json`:

```json
{
  "root": "/srv/clawinn-preview",
  "repository": "cyl19970726/TokenToEarn",
  "sourceRepo": "/srv/clawinn-preview-source.git",
  "storefrontDomain": "preview.clawinn.xyz",
  "adminDomain": "preview-admin.clawinn.xyz",
  "caddyAdmin": "http://127.0.0.1:2019",
  "caddyServer": "clawinn_preview",
  "maxPreviews": 6,
  "builder": "clawinn-preview",
  "builderEndpoint": "tcp://127.0.0.1:1234",
  "builderContainer": "clawinn-preview-buildkit",
  "buildMemory": 4294967296,
  "buildCpuQuota": 200000,
  "buildCpuPeriod": 100000,
  "dockerDataRoot": "/var/lib/docker",
  "minFreeBytes": 10737418240,
  "memory": "768m",
  "cpus": "1",
  "pids": 128,
  "uid": 1000,
  "gid": 1000,
  "port": 8080,
  "healthTimeout": 90,
  "commandTimeout": 1800
}
```

Optional `githubTokenFile` names a private file containing a read-only repository token for GitHub PR reads. No token is passed to Docker or builds. This is a private repository. Configure a read-only Git credential helper in `/srv/clawinn-preview/home/.gitconfig` for the fixed HTTPS origin (and trust only this manager-owned bare clone); store its token in a private host-only file, never in the origin URL, command line, request or logs. Git uses this dedicated HOME with interactive prompts disabled. The GitHub API token file does not automatically authenticate Git. Never pass Git credentials into the builder or application containers. Optional `syntheticRoot` and **separate** `devBaseline` are absolute operator-configured paths for a synthetic development environment and its saved `schema` descriptor. Omit both to disable shared/copy creation. Do not point either at production, a production backup, or a directory containing production keys/provider credentials. The manager never creates or populates a shared source automatically.

Create a dedicated Caddy HTTPS server with `routes: []` named `clawinn_preview` through the normal operator-managed configuration. The manager GETs and atomically PUTs only that server's routes array, preserving unrelated entries. All writers to this array must coordinate with the manager's global lock; Caddy full-config reloads should be performed outside preview activity. No Carbon routes/server are touched. A single route for both hostnames injects `X-Clawinn-Preview-Sha` and proxies to the Docker-assigned loopback port. The app supplies `X-Clawinn-App-Sha`; both are verified after publishing.

### SSH launcher

Install `clawinn-preview-request` root-owned mode 0755 in `/usr/local/sbin`:

```sh
#!/bin/sh
exec /usr/bin/python3 -I /opt/preview-server/clawinn/preview.py --config /etc/clawinn-preview/config.json request
```

Use a dedicated deploy public key with `restrict,command="/usr/local/sbin/clawinn-preview-request"` in `authorized_keys`. Reject arbitrary commands; do not evaluate `SSH_ORIGINAL_COMMAND`. Disable forwarding/PTY/password login for this account, restrict source addresses where feasible, and keep the private key only in the trusted workflow environment. If using sudo, authorize only this exact root-owned launcher with no arguments; do not authorize arbitrary Python, shells, or Docker commands. Manager exit 1 indicates failure; stdout is its JSON response.

## Data behavior

| Labels | Mode | Behavior |
| --- | --- | --- |
| none or `preview:db-isolated` | `isolated` | Initialize synthetic data once, retain on push |
| `preview:db-dev-copy` | `dev-copy` | Consistent copy of configured synthetic source once, retain on push |
| `preview:db-dev-shared` | `dev-shared` | Use configured synthetic source directly, never migrate it |

Conflicting mode labels fail for deployment/reset/refresh. `stop` ignores conflicting labels so PR closure can always clean up, and reports the previous receipt's mode (or isolated if none). Isolated and dev-copy have separate persistent `pr-N/slots/<mode>/data` roots. Switching modes retains both. A normal push keeps existing business records; an existing dev-copy does not require the current shared source baseline to match before its own migration. `refresh-dev-copy` archives the previous dev-copy then copies the configured synthetic source. `reset-db` archives the selected private directory and initializes/copies a replacement; it always refuses shared mode. Archives are private and retained, not deleted. `stop` removes the app, Caddy route and PR network, preserving data, images, receipts and archives.

The runtime image must include `node deploy/preview/app.cjs schema|init|check|migrate|copy|serve`. `PREVIEW_ROOT=/preview`; `copy --from /source --to /preview` uses SQLite's consistent backup API plus keys, receipts and schema/marker. No host SQL/code executes. On private updates the candidate builds while old web serves; then routing is removed and old web stops before the previous slot image copies the DB+keys to a private pre-update backup. Only then does the candidate explicitly migrate. No old-image rollback occurs if migration/start fails. A failed post-migration preview stays stopped; inspect the private receipt and backup and use an explicit reviewed recovery procedure.

Shared/copy initialization requires `secrets/synthetic.json`, a current descriptor identical to the separate saved baseline, and matching `formatVersion`, `sourceHash`, complete migration list and physical `schemaHash` from candidate `schema`. Candidate `check` validates the full applied schema read-only; shared data is never initialized, seeded or migrated. The synthetic source is mounted fully read-only for checks/copies. SQLite may refuse cold read-only WAL databases without existing sidecars; prepare that source with a trusted image or keep its synthetic service running. Such failures fail closed. Pre-update backups of a stopped private slot run the prior trusted image with a writable source mount solely to permit SQLite sidecars; its DB connection remains read-only. Private init/migrate validate schema internally before closing; serve validates again before accepting HTTP.

Every web container runs only `serve`, as nonroot, read-only root filesystem, read-only secrets, `/tmp` tmpfs, capabilities dropped, `no-new-privileges`, bounded memory/CPU/PIDs/logs and a per-PR internal bridge network. Helper containers have no network. Web ports bind dynamically to `127.0.0.1`; web has no outbound network by default. Do not attach other workloads to these PR networks. Validate Docker's host publishing/internal-network behavior on the chosen Engine version before enabling the workflow. Enforce host/cloud firewall isolation against host/private IP access too; a Docker network alone does not isolate the kernel or host bridge endpoints.

The app helper disables workers, mail, real payments, external providers, background jobs and production configuration; the manager supplies no production environment or env-file. Shared secrets are accessible only when that mode is explicitly requested and independently validated against live PR labels. A reviewed same-repository PR can still read all data mounted into its own container, so the shared source must contain synthetic data and development-only keys.

All requests take a per-PR flock and a global lifecycle/build flock. This conservative serialization enforces one build globally, capacity accounting and atomic Caddy read/modify/write, and prevents simultaneous manager operations on shared data. It does not pause an independently running development service, whose schema/baseline must remain stable under operator control. There is no automatic eviction. Capacity overflow fails without stopping the existing preview. SIGTERM/SIGINT attempt cleanup, and cleanup tries all app/route/network operations even if one fails. SIGKILL/host failure requires inspecting the durable receipt and reconciling through a later request; backups and data are retained.

## Verification

```sh
python3 -m unittest discover -s clawinn -p 'test_*.py' -v
bash tests/run.sh
```

Tests mock Git, Docker and HTTP; they do not build images or contact a live host. Before operational enablement, validate rootless builds, private backup/migration, restart persistence, public wildcard TLS, both SHA health headers and Docker network isolation on the dedicated Oracle host using synthetic data. Unit tests do not establish that external deployment acceptance.
