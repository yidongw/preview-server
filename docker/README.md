# Generic Docker PR previews

A configurable Linux deployment manager for GitHub PRs. Each installed project configuration names one allowed repository and owns a separate data root, resource namespace, Caddy server and SSH launcher. Repository-specific initialization, schema, migration and startup code stays in the application repository.

The existing Carbon Mac/PM2 scripts continue to work independently.

## Trusted request interface

Install a reviewed, pushed revision of this repository. Send one JSON object to a fixed SSH forced command:

```sh
python3 -I /opt/preview-server/docker/preview.py --config /etc/preview-server/widgets.json request
```

```json
{"repository":"example/widgets","prNumber":42,"sha":"0123456789abcdef0123456789abcdef01234567","action":"deploy","labels":[]}
```

Actions are `deploy`, `stop`, `reset-db`, and `refresh-dev-copy`. Requests cannot choose paths, commands, environment, domains, repositories outside the configured allowlist, or credentials. The trusted caller must verify repository write permission; never give deployment credentials to PR code or fork workflows.

A trusted GitHub Actions caller may include `githubToken` containing its short-lived repository-scoped Actions token. It travels only over the authenticated SSH stdin, stays in manager memory, and is supplied solely to GitHub metadata requests and the Git fetch subprocess environment. It is never written to config, journal, receipt, command-line arguments, build context or runtime environment. This avoids provisioning a long-lived host token. Omit it to use operator-configured host credentials.

The manager independently checks live PR state, same-repository head, configured base branch, SHA and database labels. It fetches the exact pull ref, builds only that pushed commit, and repeats checks before publishing. GitHub read failures fail closed. No extra enable label is required.

Ready responses contain named endpoint URLs:

```json
{"state":"ready","prNumber":42,"sha":"0123456789abcdef0123456789abcdef01234567","databaseMode":"isolated","urls":{"web":"https://pr-42.preview.example.com","admin":"https://pr-42.preview-admin.example.com"}}
```

The caller uses the response to update a GitHub check/comment or notify its own chat system. The manager has no chat-platform credentials. Private journal and receipt files record actual stages; `ready` requires every configured endpoint to pass health and revision checks. Errors do not forward arbitrary build or application output.

## Project configuration

Required fields include `project`, `repository`, `root`, `sourceRepo`, `dockerfile`, `helperCommand`, `serveCommand`, and at least one named `endpoints` entry. There are no repository, domain, framework or Dockerfile defaults tied to an application.

```json
{
  "project": "widgets",
  "repository": "example/widgets",
  "baseBranch": "main",
  "root": "/srv/preview-server/widgets",
  "sourceRepo": "/srv/preview-server/widgets-source.git",
  "dockerfile": "Dockerfile",
  "buildTarget": "runtime",
  "helperCommand": ["python3", "/app/preview.py"],
  "serveCommand": ["python3", "/app/preview.py", "serve"],
  "endpoints": [
    {"name": "web", "domain": "preview.example.com", "originEnv": "APP_ORIGIN"},
    {"name": "admin", "domain": "preview-admin.example.com", "originEnv": "ADMIN_ORIGIN"}
  ],
  "runtimeEnv": {"PORT": "8080", "HOST": "0.0.0.0", "JOBS_ENABLED": "false"},
  "port": 8080,
  "healthPath": "/health",
  "revisionHeader": "X-Preview-App-Sha",
  "secretsDirectory": "secrets",
  "provenanceFile": "secrets/synthetic.json",
  "descriptorFile": "secrets/schema.json",
  "descriptorFields": ["formatVersion", "sourceHash", "migrations", "schemaHash"],
  "caddyAdmin": "http://127.0.0.1:2019",
  "caddyServer": "widgets_previews",
  "builder": "preview",
  "builderContainer": "preview-buildkit",
  "builderEndpoint": "tcp://127.0.0.1:1234",
  "buildMemory": 3221225472,
  "buildCpuQuota": 100000,
  "buildCpuPeriod": 100000,
  "dockerDataRoot": "/var/lib/docker",
  "minFreeBytes": 8589934592,
  "maxPreviews": 3,
  "memory": "512m",
  "cpus": "0.5",
  "pids": 128,
  "uid": 1000,
  "gid": 1000,
  "healthTimeout": 90,
  "commandTimeout": 1800,
  "forbiddenHosts": ["control-host"]
}
```

`originEnv` is optional. Each endpoint becomes `https://pr-N.<domain>` and can inject its URL using that environment variable. Commands are argument arrays executed inside the image, with no shell interpolation. `PREVIEW_ROOT=/preview`, `PREVIEW_SHA`, `PREVIEW_PORT` and `PREVIEW_DATABASE_MODE` are supplied by the manager. Configure application-specific variable names through `runtimeEnv` and `originEnv`.

Use a separate root/config/launcher and unique `project` for each repository, so identical PR numbers cannot collide. Projects may share a bounded BuildKit instance, but each manager's lifecycle lock only serializes its own root; use one build worker or an external shared scheduler if sharing a builder. `maxPreviews` applies to that project namespace.

## Application adapter contract

The application repository supplies its adapter and includes it in the runtime image. The configured `helperCommand` accepts these appended commands:

| Command | Responsibility |
| --- | --- |
| `schema` | Return JSON describing the code's data schema |
| `init` | Initialize an empty `/preview` using synthetic data and private development keys |
| `check` | Check existing data/schema without migration or seeding |
| `migrate` | Explicitly upgrade private data and save its updated schema descriptor |
| `copy --from /source --to /preview` | Make a consistent data copy with paired keys and referenced attachments |

`serveCommand` starts the application, verifies the existing data, and exposes a health endpoint returning the requested `PREVIEW_SHA` in the configured `revisionHeader`. Shared data must never be automatically migrated or seeded at startup. Database choice and credentials are owned by the adapter, not hardcoded in the manager. The adapter must disable real payment, email, procurement and other external effects for synthetic previews.

The manager mounts the data environment at `/preview`, overlays its configured secrets directory read-only for runtime, and supplies no production credentials. Helpers run with no network. Arbitrary application secrets must not be put in `runtimeEnv`.

## Persistent data modes

| PR labels | Behavior |
| --- | --- |
| none or `preview:db-isolated` | Initialize once, preserve on push |
| `preview:db-dev-copy` | Copy configured synthetic dev data once, then preserve independently |
| `preview:db-dev-shared` | Use configured synthetic dev data directly, without migration |

Conflicting labels reject deployment. `stop` still cleans up even if labels conflict. Separate isolated/copy slots persist across mode changes. Reset/refresh archive previous private data rather than deleting it; PR commands cannot reset shared data. Stop removes services, routes and networks while retaining data.

Optional `syntheticRoot` and `devBaseline` identify an operator-managed synthetic source and separate saved descriptor. Shared/copy initialization compares the configured descriptor fields with candidate output; the adapter must also check provenance and actual database compatibility. Omit these fields to disable shared/copy creation. Never point them at production or production backups.

On private updates, build before interrupting the old app; then remove its route and stop it, use the prior slot image to copy a consistent recovery snapshot, migrate with the candidate, and start. A post-migration failure does not automatically restart an old image against changed data. Cold SQLite WAL sources mounted fully read-only may need trusted sidecar preparation; the adapter must fail closed rather than ignore committed WAL data.

## Host preparation

Install Python 3.9+, Git, Docker Engine 25+, buildx and Caddy on the deployment host. Never build on a control host; configure its hostname in `forbiddenHosts` or install `/run/preview-control-host`. Existing operator-specific markers can be selected using `controlHostMarker`.

Install code/config/launcher root-owned, with private configuration, bare repository, journal and data directories. Use a dedicated manager HOME under `<root>/home`. A private repository needs its own read-only Git credential helper in that HOME; optional `githubTokenFile` separately supplies a host-only read token for GitHub metadata. Neither credential reaches the builder or runtime. The bare origin must be fixed to the configured repository.

Use a rootless, nonprivileged BuildKit instance and buildx remote driver. Example operator provisioning (pin a reviewed digest):

```sh
docker run -d --name preview-buildkit --restart unless-stopped \
  --user 1000:1000 --memory 3g --memory-swap 3g \
  --cpu-period 100000 --cpu-quota 100000 --pids-limit 512 \
  --security-opt seccomp=unconfined --security-opt apparmor=preview-buildkit \
  --mount type=volume,source=preview-buildkit,target=/home/user/.local/share/buildkit \
  --publish 127.0.0.1:1234:1234 \
  moby/buildkit:rootless@sha256:REPLACE_WITH_REVIEWED_DIGEST \
  --addr tcp://0.0.0.0:1234 --oci-worker-no-process-sandbox
HOME=/srv/preview-server/widgets/home docker buildx create --name preview \
  --driver remote tcp://127.0.0.1:1234
```

Provision a suitable container-specific AppArmor profile and user namespaces; do not disable host-wide protections. Keep the unauthenticated builder endpoint loopback-only, limit parallelism/cache, and enforce disk quotas/GC. The manager verifies builder identity, non-root user, CPU and memory bounds. A free-space floor is not a disk quota. Never fall back to a privileged builder or globally prune shared Docker data. Preserve helper image versions referenced by data slots.

Configure wildcard DNS and Caddy TLS for the project's domains. Caddy can obtain exact-host certificates using HTTP-01 when routes are added; persist certificate state and allow 80/443. Each project uses a Caddy HTTPS server with `routes: []`. The manager atomically PATCHes that server's route array and adds `X-Preview-Sha`. Other route writers must coordinate with its global lock.

Runtime uses a per-PR internal bridge with no published ports. Trusted host-network Caddy proxies to the verified private container IP. The manager rejects extra network attachments, noninternal bridges, addresses outside their subnet and published ports. Verify blocked egress and host-to-container routing on the target Docker version. Containers run nonroot with read-only rootfs/secrets, dropped capabilities, no-new-privileges, tmpfs, bounded CPU/memory/PIDs/logs. They must not receive Docker sockets or deployment credentials. Isolate the deployment host from production/private/control networks; reviewed same-repository PRs can still read their own mounted synthetic data.

Install a dedicated forced-command key using a root-owned launcher derived from `preview-request`, fixed to one project config. Never evaluate `SSH_ORIGINAL_COMMAND`; disable forwarding and PTY. Do not authorize arbitrary Python, shell or Docker through sudo.

## Verification

```sh
python3 -m unittest discover -s docker -p 'test_*.py' -v
bash tests/run.sh
```

Tests use a fictitious Python application and mock infrastructure. They verify request validation, data lifecycle, schema guards, named endpoints, namespace separation, routing and container constraints. Actual build, startup, data persistence, public TLS and notification delivery require live acceptance in each integrating project.
