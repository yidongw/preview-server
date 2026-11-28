#!/usr/bin/env python3
"""Trusted Linux entry point. PR source runs only in constrained Docker containers."""
import argparse
import base64
import contextlib
import ipaddress
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid


class PreviewError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise PreviewError(message)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream)


@contextlib.contextmanager
def lock(path):
    with Path(path).open('a') as stream:
        os.chmod(path, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def database_mode(labels):
    selected = set(labels) & {'preview:db-isolated', 'preview:db-dev-copy', 'preview:db-dev-shared'}
    require(len(selected) <= 1, 'conflicting database labels')
    return next(iter(selected), 'preview:db-isolated').removeprefix('preview:db-')


def validate_request(value, repository):
    require(isinstance(value, dict), 'request must be an object')
    require(set(value) in ({'repository', 'prNumber', 'sha', 'action', 'labels'},
                           {'repository', 'prNumber', 'sha', 'action', 'labels', 'githubToken'}), 'unexpected request fields')
    if 'githubToken' in value:
        require(isinstance(value['githubToken'], str) and 20 <= len(value['githubToken']) <= 4096
                and re.fullmatch(r'[\x21-\x7e]+', value['githubToken']), 'invalid GitHub credential')
    require(value['repository'] == repository, 'repository is not allowed')
    require(value['action'] in ('deploy', 'stop', 'reset-db', 'refresh-dev-copy', 'warm'), 'invalid action')
    if value['action'] == 'warm':
        # Prepare the base branch's dependencies and build outputs, so a new PR's first deployment reuses them.
        require(type(value['prNumber']) is int and value['prNumber'] == 0, 'warm takes PR number 0')
    else:
        require(type(value['prNumber']) is int and 0 < value['prNumber'] < 2**31, 'invalid PR number')
    require(isinstance(value['sha'], str) and re.fullmatch('[0-9a-f]{40}', value['sha']), 'invalid SHA')
    require(isinstance(value['labels'], list) and len(value['labels']) <= 100
            and all(isinstance(label, str) and len(label) <= 100 for label in value['labels']), 'invalid labels')
    if value['action'] == 'warm':
        require(value['labels'] == [], 'warm takes no labels')
        return None
    if value['action'] == 'stop':
        # Closing must clean up even if someone left conflicting mode labels behind.
        return 'isolated'
    mode = database_mode(value['labels'])
    require(value['action'] != 'reset-db' or mode != 'dev-shared', 'cannot reset shared development data')
    require(value['action'] != 'refresh-dev-copy' or mode == 'dev-copy', 'refresh requires dev-copy mode')
    return mode


def config_from(path):
    config = read_json(path)
    defaults = dict(caddyAdmin='http://127.0.0.1:2019', caddyServer='previews', maxPreviews=6,
                    docker='docker', git='git', builder='preview', builderContainer='preview-buildkit',
                    builderEndpoint='tcp://127.0.0.1:1234', buildMemory=4294967296,
                    buildCpuQuota=200000, buildCpuPeriod=100000, minFreeBytes=10737418240,
                    dockerDataRoot='/var/lib/docker', memory='768m', cpus='1', pids=128,
                    uid=1000, gid=1000, port=8080, healthTimeout=90, commandTimeout=1800,
                    baseBranch='main', buildTarget='runtime', healthPath='/api/health',
                    revisionHeader='X-Preview-App-Sha', runtimeEnv={}, secretsDirectory='secrets',
                    provenanceFile='secrets/synthetic.json', descriptorFile='secrets/schema.json',
                    descriptorFields=['formatVersion', 'sourceHash', 'migrations', 'schemaHash'],
                    forbiddenHosts=[], controlHostMarker='/run/preview-control-host')
    defaults.update(config)
    config = defaults
    require(isinstance(config.get('repository'), str) and
            re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', config['repository']), 'invalid repository')
    require(isinstance(config.get('project'), str) and re.fullmatch(r'[a-z][a-z0-9-]{0,39}', config['project']), 'invalid project namespace')
    for field in ('root', 'sourceRepo', 'dockerDataRoot'):
        require(isinstance(config.get(field), str) and Path(config[field]).is_absolute(), 'invalid config path: ' + field)
    endpoints = config.get('endpoints')
    require(isinstance(endpoints, list) and 0 < len(endpoints) <= 20, 'endpoints are required')
    names, domains = set(), set()
    for endpoint in endpoints:
        require(isinstance(endpoint, dict) and set(endpoint) <= {'name', 'domain', 'originEnv'}, 'invalid endpoint')
        name, domain = endpoint.get('name'), endpoint.get('domain')
        require(isinstance(name, str) and re.fullmatch(r'[a-z][a-z0-9_-]*', name) and name not in names, 'invalid or duplicate endpoint name')
        require(isinstance(domain, str) and re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*', domain) and '.' in domain and domain not in domains, 'invalid or duplicate endpoint domain')
        names.add(name)
        domains.add(domain)
        if 'originEnv' in endpoint:
            require(re.fullmatch(r'[A-Z_][A-Z0-9_]*', endpoint['originEnv']), 'invalid origin environment name')
    for field in ('caddyServer', 'builder'):
        require(isinstance(config[field], str) and re.fullmatch('[a-zA-Z0-9_-]+', config[field]), 'invalid ' + field)
    require(re.fullmatch(r'http://127\.0\.0\.1:[0-9]+', config['caddyAdmin']), 'Caddy admin must be loopback')
    for field in ('maxPreviews', 'buildMemory', 'buildCpuQuota', 'buildCpuPeriod', 'minFreeBytes', 'uid', 'gid', 'pids', 'port', 'healthTimeout', 'commandTimeout'):
        require(type(config[field]) is int and config[field] > 0, 'invalid positive limit: ' + field)
    for field in ('dockerfile', 'secretsDirectory', 'provenanceFile', 'descriptorFile'):
        value = config.get(field)
        require(isinstance(value, str) and value and not value.startswith('/') and
                '..' not in Path(value).parts and Path(value).as_posix() == value and ',' not in value, 'invalid relative path: ' + field)
    for field in ('helperCommand', 'serveCommand'):
        command = config.get(field)
        require(isinstance(command, list) and command and all(isinstance(arg, str) and arg and '\0' not in arg for arg in command), field + ' is required')
    require(isinstance(config['runtimeEnv'], dict) and all(re.fullmatch(r'[A-Z_][A-Z0-9_]*', key) and isinstance(value, str) for key, value in config['runtimeEnv'].items()), 'invalid runtime environment')
    require(isinstance(config['healthPath'], str) and config['healthPath'].startswith('/') and not config['healthPath'].startswith('//'), 'invalid health path')
    require(isinstance(config['revisionHeader'], str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9-]*', config['revisionHeader']), 'invalid revision header')
    require(isinstance(config['descriptorFields'], list) and config['descriptorFields'] and all(isinstance(field, str) for field in config['descriptorFields']), 'invalid descriptor fields')
    require(isinstance(config['forbiddenHosts'], list) and all(isinstance(host, str) for host in config['forbiddenHosts']), 'invalid forbidden hosts')
    require(isinstance(config['controlHostMarker'], str) and Path(config['controlHostMarker']).is_absolute(), 'invalid control host marker')
    require(config.get('buildMode', 'image') in ('image', 'mount'), 'invalid buildMode')
    if config.get('buildMode') == 'mount':
        mount_config(config)
    return config


PREFIX = re.compile(r'[A-Za-z0-9._@+-][A-Za-z0-9._@+/-]*')
OUTPUT = re.compile(r'[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*')


def prefixes(value, field):
    require(isinstance(value, list) and len(value) <= 100 and all(
        isinstance(item, str) and (item == '' or (PREFIX.fullmatch(item) and '..' not in item.split('/')))
        for item in value), 'invalid path prefixes: ' + field)
    return value


def mount_config(config):
    """Mount mode: no per-commit image. A fixed base image runs the commit's tree, cached dependencies and cached
    per-step build outputs, each mounted read-only into the runtime container."""
    require(isinstance(config.get('baseImage'), str) and re.fullmatch(r'[a-z0-9][a-z0-9._/:@-]{0,254}', config['baseImage']),
            'mount mode needs baseImage')
    config.setdefault('workdir', '/app')
    require(isinstance(config['workdir'], str) and re.fullmatch(r'/[a-z0-9_/-]+', config['workdir']), 'invalid workdir')
    config.setdefault('buildCpus', '2')
    config.setdefault('buildMemoryLimit', '3g')
    config.setdefault('cacheDays', 7)
    require(re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', str(config['buildCpus'])), 'invalid buildCpus')
    require(re.fullmatch(r'[0-9]+[kmg]', str(config['buildMemoryLimit'])), 'invalid buildMemoryLimit')
    require(type(config['cacheDays']) is int and config['cacheDays'] > 0, 'invalid cacheDays')
    install = config.get('install')
    require(isinstance(install, dict) and set(install) <= {'command', 'keyFiles', 'tmpfs', 'env'}, 'mount mode needs install')
    require(isinstance(install.get('command'), list) and install['command'] and
            all(isinstance(arg, str) and arg and '\0' not in arg for arg in install['command']), 'invalid install command')
    prefixes(install.get('keyFiles'), 'install.keyFiles')
    require(install['keyFiles'], 'install.keyFiles is required')
    install.setdefault('tmpfs', [])
    install.setdefault('env', {})
    require(isinstance(install['tmpfs'], list) and all(isinstance(name, str) and re.fullmatch(r'\.?[A-Za-z0-9_-]+', name)
                                                      for name in install['tmpfs']), 'invalid install.tmpfs')
    require(isinstance(install['env'], dict) and all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) and isinstance(value, str)
                                                     for key, value in install['env'].items()), 'invalid install.env')
    steps = config.get('steps', [])
    require(isinstance(steps, list) and len(steps) <= 20, 'invalid steps')
    names, outputs = set(), []
    for step in steps:
        require(isinstance(step, dict) and set(step) <= {'name', 'command', 'inputs', 'exclude', 'outputs'}, 'invalid step')
        require(isinstance(step.get('name'), str) and re.fullmatch(r'[a-z][a-z0-9-]{0,39}', step['name']) and step['name'] not in names,
                'invalid or duplicate step name')
        names.add(step['name'])
        require(isinstance(step.get('command'), list) and step['command'] and
                all(isinstance(arg, str) and arg and '\0' not in arg for arg in step['command']), 'invalid step command')
        prefixes(step.get('inputs'), 'step inputs')
        step.setdefault('exclude', [])
        prefixes(step['exclude'], 'step exclude')
        require(isinstance(step.get('outputs'), list) and step['outputs'] and
                all(isinstance(item, str) and OUTPUT.fullmatch(item) and '..' not in item.split('/') for item in step['outputs']),
                'invalid step outputs')
        outputs += step['outputs']
    # Steps run side by side in one tree: their outputs must not overlap.
    for i, first in enumerate(outputs):
        for j, second in enumerate(outputs):
            require(i == j or not (first == second or first.startswith(second + '/')), 'step outputs overlap')
    return config


class Manager:
    def __init__(self, config):
        self.c = config
        self.root = Path(config['root'])
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.pr = None
        self.request = None
        self.receipt = None
        self.token = None
        self.build_summary = None

    def run(self, args, *, input=None, json_output=False, timeout=None, github_auth=False):
        environment = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                       'HOME': str(self.root / 'home'), 'GIT_TERMINAL_PROMPT': '0'}
        if github_auth and self.token:
            environment.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                               GIT_CONFIG_VALUE_0='Authorization: Basic ' + base64.b64encode(('x-access-token:' + self.token).encode()).decode())
        try:
            result = subprocess.run(args, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=timeout or self.c['commandTimeout'], check=True,
                                    env=environment)
        except (subprocess.SubprocessError, OSError):
            # Never forward build/helper output: it may contain credentials or PR-controlled terminal escapes.
            raise PreviewError('command failed: ' + Path(args[0]).name) from None
        if json_output:
            try:
                return json.loads(result.stdout)
            except (ValueError, UnicodeError):
                raise PreviewError('command returned invalid JSON') from None
        return result.stdout.decode('utf-8', errors='replace').strip()

    def docker(self, *args, **kwargs):
        return self.run([self.c['docker'], *args], **kwargs)

    def http(self, url, method='GET', body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method,
                                         headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with self.opener.open(request, timeout=15) as response:
                content = response.read(2 * 1024 * 1024)
                return json.loads(content) if content else None
        except (OSError, ValueError):
            raise PreviewError('HTTP request failed') from None

    def github_current(self, *, stopping=False):
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'preview-server',
                   'X-GitHub-Api-Version': '2022-11-28'}
        token = self.token
        if not token and self.c.get('githubTokenFile'):
            token = Path(self.c['githubTokenFile']).read_text().strip()
        if token:
            headers['Authorization'] = 'Bearer ' + token
        value = self.http('https://api.github.com/repos/' + self.c['repository'] + '/pulls/' + str(self.pr), headers=headers)
        require(value.get('base', {}).get('repo', {}).get('full_name') == self.c['repository'], 'wrong PR repository')
        # Stop is idempotent for closed PRs, but stale workflow events may not stop a newer open head.
        if stopping and value.get('state') == 'closed':
            return
        require(value.get('state') == 'open', 'PR is no longer open')
        require(value.get('head', {}).get('sha') == self.request['sha'], 'stale PR SHA')
        if stopping:
            return
        require(value.get('head', {}).get('repo', {}).get('full_name') == self.c['repository'], 'fork PRs are not allowed')
        require(value.get('base', {}).get('ref') == self.c['baseBranch'], 'PR targets an unsupported base branch')
        actual = [label['name'] for label in value.get('labels', [])]
        require(database_mode(actual) == database_mode(self.request['labels']), 'database labels changed')

    def fetch_current(self):
        repo = self.c['sourceRepo']
        self.run([self.c['git'], '--git-dir', repo, 'fetch', '--no-tags', 'origin',
                  '+refs/pull/%s/head:refs/previews/%s/pr-%s' % (self.pr, self.c['project'], self.pr)], github_auth=True)
        sha = self.run([self.c['git'], '--git-dir', repo, 'rev-parse', '--verify',
                        'refs/previews/%s/pr-%s^{commit}' % (self.c['project'], self.pr)])
        require(sha == self.request['sha'], 'stale PR SHA')

    @property
    def resource_prefix(self):
        return 'preview-%s-pr-' % self.c['project']

    @property
    def resource_name(self):
        return self.resource_prefix + str(self.pr)

    def urls(self):
        return {endpoint['name']: 'https://pr-%s.%s' % (self.pr, endpoint['domain'])
                for endpoint in self.c['endpoints']}

    def event(self, state, **fields):
        record = dict(time=int(time.time()), state=state, prNumber=self.pr, sha=self.request['sha'], **fields)
        journal = self.root / 'journal.jsonl'
        with journal.open('a', encoding='utf-8') as stream:
            os.chmod(journal, 0o600)
            stream.write(json.dumps(record, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        self.receipt = {**(self.receipt or {}), **record}
        atomic_json(self.pr_root / 'receipt.json', self.receipt)
        return self.receipt

    @property
    def pr_root(self):
        return self.root / ('pr-%s' % self.pr)

    def routes(self, upstream=None):
        hosts = [url.removeprefix('https://') for url in self.urls().values()]
        return {'@id': self.resource_name, 'match': [{'host': hosts}],
                'handle': [{'handler': 'headers', 'response': {'set': {'X-Preview-Sha': [self.request['sha']]}}},
                           {'handler': 'reverse_proxy', 'upstreams': [{'dial': upstream}]}],
                'terminal': True}

    def update_route(self, upstream=None):
        endpoint = self.c['caddyAdmin'] + '/config/apps/http/servers/' + self.c['caddyServer'] + '/routes'
        routes = self.http(endpoint) or []
        route_id = self.resource_name
        routes = [route for route in routes if route.get('@id') not in (route_id, 'evicted-' + route_id)]
        if upstream is not None:
            live = sum(str(route.get('@id', '')).startswith(self.resource_prefix) for route in routes)
            require(live < self.c['maxPreviews'], 'preview capacity reached')
            routes.insert(0, self.routes(upstream))
        # PATCH replaces an existing key; PUT creates and rejects existing routes with HTTP 409.
        self.http(endpoint, method='PATCH', body=routes)

    def check_capacity(self):
        """Called under the project lifecycle lock, after candidate validation, before disruption."""
        endpoint = self.c['caddyAdmin'] + '/config/apps/http/servers/' + self.c['caddyServer'] + '/routes'
        routes = self.http(endpoint) or []
        live = [route for route in routes if str(route.get('@id', '')).startswith(self.resource_prefix)
                and route.get('@id') != self.resource_name]
        evicted = []
        while len(live) >= self.c['maxPreviews']:
            # Failed update receipts overwrite their time/state while the old app stays live. Use the latest successful
            # deployment from the durable journal, not the PR number, receipt mtime, or most recent failed request.
            ready = {}
            journal = self.root / 'journal.jsonl'
            if journal.exists():
                for line in journal.read_text().splitlines():
                    record = json.loads(line)
                    if record.get('state') == 'ready':
                        ready[record['prNumber']] = record
            candidates = []
            for route in live:
                match = re.fullmatch(re.escape(self.resource_prefix) + r'([1-9][0-9]*)', route.get('@id', ''))
                require(match is not None, 'invalid active preview route')
                number = int(match[1])
                record = ready.get(number)
                require(record is not None and type(record.get('time')) is int
                        and re.fullmatch(r'[0-9a-f]{40}', str(record.get('sha', ''))),
                        'active preview has no successful deployment record')
                candidates.append((record['time'], number, record))
            _, number, record = min(candidates)
            victim = Manager(self.c)
            victim.pr, victim.request = number, dict(prNumber=number, sha=record['sha'])
            receipt = victim.pr_root / 'receipt.json'
            victim.receipt = read_json(receipt) if receipt.exists() else {}
            failed = False
            # Do not acquire another per-PR lock: another request may already hold it while waiting for our global lock.
            # The global project lock serializes every lifecycle mutation, including this cross-PR stop.
            for cleanup in (victim.update_route, victim.stop_app, victim.remove_network):
                try:
                    cleanup()
                except Exception:
                    failed = True
            if failed:
                victim.event('eviction-failed', cleanupRequired=True, evictedBy=self.pr)
                raise PreviewError('oldest preview eviction needs cleanup')
            routes = self.http(endpoint) or []
            routes.insert(0, {'@id': 'evicted-' + victim.resource_name,
                             'match': [{'host': [url.removeprefix('https://') for url in victim.urls().values()]}],
                             'handle': [{'handler': 'static_response', 'status_code': 410,
                                         'body': 'This preview was stopped to make room for a newer deployment. '
                                                 'Its data is retained. Redeploy this PR to restore it.\n'}],
                             'terminal': True})
            self.http(endpoint, method='PATCH', body=routes)
            victim.event('evicted', cleanupRequired=False, evictedBy=self.pr)
            evicted.append(number)
            live = [route for route in live if route.get('@id') != victim.resource_name]
        return evicted

    def build(self):
        require(shutil.disk_usage(self.c['dockerDataRoot']).free >= self.c['minFreeBytes'], 'insufficient Docker disk space')
        if self.c.get('buildMode') == 'mount':
            return self.build_mount()
        # buildx inspect has no --format on current releases. ls emits one JSON object per builder.
        records = self.docker('buildx', 'ls', '--format', '{{json .}}')
        try:
            builders = [json.loads(line) for line in records.splitlines() if line.strip()]
        except (ValueError, AttributeError):
            raise PreviewError('invalid builder listing') from None
        # Some buildx installations repeat identical records for discovered contexts.
        unique = {json.dumps(item, sort_keys=True): item for item in builders if item.get('Name') == self.c['builder']}
        matches = list(unique.values())
        require(len(matches) == 1, 'configured builder is missing or ambiguous')
        builder = matches[0]
        require(builder.get('Driver') == 'remote' and len(builder.get('Nodes', [])) == 1
                and builder['Nodes'][0].get('Endpoint') == self.c['builderEndpoint'], 'unexpected build endpoint')
        info = self.docker('inspect', self.c['builderContainer'], json_output=True)[0]
        limits = info.get('HostConfig', {})
        user = str(info.get('Config', {}).get('User', ''))
        require(not limits.get('Privileged') and re.fullmatch(r'[1-9][0-9]*(?::[1-9][0-9]*)?', user),
                'builder must be non-root and unprivileged')
        require(0 < limits.get('Memory', 0) <= self.c['buildMemory'], 'builder memory must be bounded')
        quota, period = limits.get('CpuQuota', 0), limits.get('CpuPeriod', 0)
        require(quota > 0 and period > 0 and quota / period <= self.c['buildCpuQuota'] / self.c['buildCpuPeriod'],
                'builder CPU must be bounded')
        # Git archive emits a tar stream; no PR paths or source are extracted on this host.
        image = 'preview-%s:pr-%s-%s' % (self.c['project'], self.pr, self.request['sha'])
        self.build_context(image)
        return image

    def build_context(self, image):
        git_args = [self.c['git'], '--git-dir', self.c['sourceRepo'], 'archive', '--format=tar', self.request['sha']]
        docker_args = [self.c['docker'], 'buildx', 'build', '--builder', self.c['builder'], '--load',
                       '--target', self.c['buildTarget'], '--file', self.c['dockerfile'],
                       '--build-arg', 'GIT_COMMIT=' + self.request['sha'], '--tag', image, '-']
        env = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
               'HOME': str(self.root / 'home'), 'GIT_TERMINAL_PROMPT': '0'}
        # Keep stderr out of host logs and out of memory; only a sanitized failure reaches the caller.
        with open(os.devnull, 'wb') as sink:
            try:
                with subprocess.Popen(git_args, stdout=subprocess.PIPE, stderr=sink, env=env) as archive:
                    try:
                        result = subprocess.run(docker_args, stdin=archive.stdout, stdout=sink, stderr=sink,
                                                env=env, timeout=self.c['commandTimeout'])
                        archive.stdout.close()
                        archive.wait(timeout=30)
                        require(result.returncode == 0 and archive.returncode == 0, 'image build failed')
                    finally:
                        if archive.poll() is None:
                            archive.kill()
            except (OSError, subprocess.SubprocessError):
                raise PreviewError('image build failed') from None
        metadata = self.docker('image', 'inspect', image, json_output=True)[0]
        require(metadata.get('Config', {}).get('Labels', {}).get('org.opencontainers.image.revision') == self.request['sha'],
                'image revision does not match request')

    # Mount mode ------------------------------------------------------------------------------------------------
    # The deployment handle saved in a slot's image.json is "mount:<commit>:<dependency key>" instead of an image tag.
    # Everything under <root>/mount is content-addressed: a source tree per commit, one dependency install per key
    # (base image, install command and the key files' blobs), and one output set per build step and key (the step,
    # the dependency key and its input files' blobs). PR code only ever runs inside the constrained containers below;
    # the host extracts nothing itself and only lists, renames and deletes those directories.

    @property
    def mount_root(self):
        return self.root / 'mount'

    def tree(self, sha):
        listing = self.run([self.c['git'], '--git-dir', self.c['sourceRepo'], 'ls-tree', '-r', '-z', '--full-tree', sha])
        entries = {}
        for record in listing.split('\0'):
            if record:
                meta, _, path = record.partition('\t')
                entries[path] = meta
        require(entries, 'empty source tree')
        return entries

    @staticmethod
    def selected(entries, include, exclude=()):
        def hit(path, items):
            return any(path.startswith(item) for item in items)
        return sorted([path, meta] for path, meta in entries.items() if hit(path, include) and not hit(path, exclude))

    @staticmethod
    def digest(*parts):
        return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def build_sandbox(self):
        # Install and build containers: like the runtime sandbox, with build-sized limits and no container log file.
        return ['--read-only', '--user', '%s:%s' % (self.c['uid'], self.c['gid']),
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--pids-limit', '2048',
                '--cpus', str(self.c['buildCpus']), '--memory', self.c['buildMemoryLimit'],
                '--memory-swap', self.c['buildMemoryLimit'], '--tmpfs', '/tmp:rw,nosuid,size=1g',
                '--log-driver', 'none', '--env', 'HOME=/tmp', '--env', 'CI=true']

    def environment(self):
        return {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                'HOME': str(self.root / 'home'), 'GIT_TERMINAL_PROMPT': '0'}

    def extract(self, sha, target, paths=None):
        """git archive on the host, tar inside a networkless container as the service user."""
        git_args = [self.c['git'], '--git-dir', self.c['sourceRepo'], 'archive', '--format=tar', sha]
        if paths:
            git_args += ['--', *[':(literal)' + path for path in paths]]
        docker_args = [self.c['docker'], 'run', '--rm', '-i', '--network', 'none', *self.build_sandbox(),
                       *self.mount(target, '/out'), '--entrypoint', 'tar', self.c['baseImage'],
                       '-x', '-f', '-', '-C', '/out', '--no-same-owner']
        with open(os.devnull, 'wb') as sink:
            try:
                with subprocess.Popen(git_args, stdout=subprocess.PIPE, stderr=sink, env=self.environment()) as archive:
                    try:
                        result = subprocess.run(docker_args, stdin=archive.stdout, stdout=sink, stderr=sink,
                                                env=self.environment(), timeout=self.c['commandTimeout'])
                        archive.stdout.close()
                        archive.wait(timeout=30)
                        require(result.returncode == 0 and archive.returncode == 0, 'source extraction failed')
                    finally:
                        if archive.poll() is None:
                            archive.kill()
            except (OSError, subprocess.SubprocessError):
                raise PreviewError('source extraction failed') from None

    def module_args(self, key, modules, tmpfs=False):
        base, workdir = self.mount_root / 'deps' / key, self.c['workdir']
        args = []
        for module in modules:
            require(OUTPUT.fullmatch(module) and '..' not in module.split('/'), 'invalid dependency directory')
            args += self.mount(base / module, workdir + '/' + module, True)
            if tmpfs:
                # Build tools write small temporary files next to their packages (Vite: node_modules/.vite-temp).
                for name in self.c['install']['tmpfs']:
                    args += ['--tmpfs', '%s/%s/%s:rw,nosuid,size=256m,uid=%s,gid=%s'
                             % (workdir, module, name, self.c['uid'], self.c['gid'])]
        return args

    def ensure_deps(self, sha, entries):
        install = self.c['install']
        files = self.selected(entries, install['keyFiles'])
        require(files, 'no dependency key files in source tree')
        key = self.digest('deps', self.c['baseImage'], install['command'], install['env'], install['tmpfs'], files)
        final = self.mount_root / 'deps' / key
        if (final / 'manifest.json').is_file():
            os.utime(final)
            return key, read_json(final / 'manifest.json')['modules'], False
        self.owned_dir(self.mount_root / 'deps')
        cache = self.owned_dir(self.mount_root / 'package-cache')
        work = self.owned_dir(self.mount_root / 'deps' / ('.tmp-' + key + '-' + uuid.uuid4().hex))
        try:
            self.extract(sha, work, [path for path, _ in files])
            env = [arg for name, value in sorted(install['env'].items()) for arg in ('--env', name + '=' + value)]
            # The only container with network access: package installs need the registry. It sees the package
            # manifests and the shared package cache, not the source tree, data or any credential.
            self.docker('run', '--rm', *self.build_sandbox(), *self.mount(work, '/work'), *self.mount(cache, '/cache'),
                        *env, '--workdir', '/work', '--entrypoint', install['command'][0], self.c['baseImage'],
                        *install['command'][1:])
            modules = []
            for current, dirs, _ in os.walk(work):
                if 'node_modules' in dirs:
                    path = Path(current) / 'node_modules'
                    if not path.is_symlink():
                        modules.append(path.relative_to(work).as_posix())
                    dirs.remove('node_modules')
            require(modules, 'dependency install produced no node_modules')
            for module in modules:
                for name in install['tmpfs']:
                    point = work / module / name
                    require(not point.is_symlink() and (point.is_dir() or not point.exists()), 'invalid tmpfs mount point')
                    point.mkdir(exist_ok=True)
            atomic_json(work / 'manifest.json', {'modules': sorted(modules)})
            os.rename(work, final)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        return key, sorted(modules), True

    def run_steps(self, work, key, modules, entries):
        steps_root = self.owned_dir(self.mount_root / 'steps')
        hits, misses, summary = [], [], {}
        for step in self.c.get('steps', []):
            files = self.selected(entries, step['inputs'], step['exclude'])
            step_key = self.digest('step', step['name'], step['command'], step['outputs'], self.c['baseImage'], key, files)
            cached = steps_root / step['name'] / step_key
            self.owned_dir(steps_root / step['name'])
            if (cached / '.complete').is_file():
                os.utime(cached)
                hits.append((step, step_key))
            else:
                misses.append((step, step_key))
            summary[step['name']] = 'cached' if (cached / '.complete').is_file() else 'built'
        workdir = self.c['workdir']
        if hits:
            script = ' && '.join('cp -a /cache/%s/%s/files/. %s/' % (step['name'], step_key, workdir) for step, step_key in hits)
            self.docker('run', '--rm', '--network', 'none', *self.build_sandbox(), *self.mount(steps_root, '/cache', True),
                        *self.mount(work, workdir), '--entrypoint', 'sh', self.c['baseImage'], '-c', script)
        if misses:
            processes = []
            with open(os.devnull, 'wb') as sink:
                try:
                    for step, _ in misses:
                        args = [self.c['docker'], 'run', '--rm', '--network', 'none', *self.build_sandbox(),
                                *self.mount(work, workdir), *self.module_args(key, modules, tmpfs=True),
                                '--workdir', workdir, '--entrypoint', step['command'][0], self.c['baseImage'], *step['command'][1:]]
                        processes.append(subprocess.Popen(args, stdout=sink, stderr=sink, env=self.environment()))
                    deadline = time.monotonic() + self.c['commandTimeout']
                    for (step, _), process in zip(misses, processes):
                        code = process.wait(timeout=max(1, deadline - time.monotonic()))
                        require(code == 0, 'build step failed: ' + step['name'])
                except (OSError, subprocess.SubprocessError):
                    raise PreviewError('build step failed') from None
                finally:
                    for process in processes:
                        if process.poll() is None:
                            process.kill()
                            process.wait()
            parts = []
            for step, step_key in misses:
                temporary = '/cache/%s/%s.tmp' % (step['name'], step_key)
                part = ['rm -rf ' + temporary, 'mkdir -p %s/files' % temporary]
                for output in step['outputs']:
                    parent = os.path.dirname(output)
                    part.append('test -e %s/%s' % (workdir, output))
                    if parent:
                        part.append('mkdir -p %s/files/%s' % (temporary, parent))
                    part.append('cp -a %s/%s %s/files/%s' % (workdir, output, temporary, output))
                part.append('rm -rf /cache/%s/%s && mv %s /cache/%s/%s' % (step['name'], step_key, temporary, step['name'], step_key))
                parts.append(' && '.join(part))
            self.docker('run', '--rm', '--network', 'none', *self.build_sandbox(), *self.mount(work, workdir, True),
                        *self.mount(steps_root, '/cache'), '--entrypoint', 'sh', self.c['baseImage'], '-c', ' && '.join(parts))
            for step, step_key in misses:
                cached = steps_root / step['name'] / step_key
                require(cached.is_dir() and not cached.is_symlink(), 'build output was not saved')
                (cached / '.complete').write_text(self.request['sha'] + '\n')
        return summary

    def build_mount(self):
        sha = self.request['sha']
        entries = self.tree(sha)
        key, modules, installed = self.ensure_deps(sha, entries)
        sources = self.owned_dir(self.mount_root / 'src')
        source, record = sources / sha, sources / (sha + '.json')
        handle = 'mount:%s:%s' % (sha, key)
        if record.is_file() and source.is_dir() and not source.is_symlink() and read_json(record).get('deps') == key:
            os.utime(source)
            self.build_summary = {'dependencies': 'cached', 'source': 'cached'}
            return handle
        work = self.owned_dir(sources / ('.tmp-' + sha + '-' + uuid.uuid4().hex))
        try:
            self.extract(sha, work)
            for module in modules:
                point = work / module
                # A committed node_modules, or a symlinked parent, would let the tree redirect a dependency mount.
                require(not point.exists() and not point.is_symlink(), 'source tree contains a dependency directory')
                parent = point.parent
                while parent != work:
                    require(not parent.is_symlink() and (parent.is_dir() or not parent.exists()), 'invalid dependency mount point')
                    parent = parent.parent
                point.mkdir(parents=True)
            steps = self.run_steps(work, key, modules, entries)
            if source.exists():
                shutil.rmtree(source)
            os.rename(work, source)
            record.unlink(missing_ok=True)
            atomic_json(record, {'sha': sha, 'deps': key, 'baseImage': self.c['baseImage'], 'steps': steps})
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        self.build_summary = {'dependencies': 'installed' if installed else 'cached', 'source': 'extracted', 'steps': steps}
        return handle

    def runtime(self, image):
        """Docker arguments that select an image or a mount handle: (mount arguments, image reference)."""
        match = re.fullmatch(r'mount:([0-9a-f]{40}):([0-9a-f]{64})', image)
        if not match:
            return [], image
        sha, key = match.groups()
        record = read_json(self.mount_root / 'src' / (sha + '.json'))
        require(record.get('sha') == sha and record.get('deps') == key, 'mounted source record does not match')
        modules = read_json(self.mount_root / 'deps' / key / 'manifest.json')['modules']
        workdir = self.c.get('workdir', '/app')
        args = [*self.mount(self.mount_root / 'src' / sha, workdir, True)]
        for module in modules:
            require(OUTPUT.fullmatch(module) and '..' not in module.split('/'), 'invalid dependency directory')
            args += self.mount(self.mount_root / 'deps' / key / module, workdir + '/' + module, True)
        return args + ['--workdir', workdir], record['baseImage']

    def fetch_base(self):
        repo, ref = self.c['sourceRepo'], 'refs/previews/%s/base' % self.c['project']
        self.run([self.c['git'], '--git-dir', repo, 'fetch', '--no-tags', 'origin',
                  '+refs/heads/%s:%s' % (self.c['baseBranch'], ref)], github_auth=True)
        sha = self.run([self.c['git'], '--git-dir', repo, 'rev-parse', '--verify', ref + '^{commit}'])
        require(sha == self.request['sha'], 'stale base SHA')

    def collect(self):
        """Best effort: drop source trees no slot uses, and dependency sets and step outputs unused for cacheDays."""
        if self.c.get('buildMode') != 'mount' or not self.mount_root.is_dir():
            return
        try:
            shas, keys = set(), set()
            for path in self.root.glob('pr-*/slots/*/image.json'):
                with contextlib.suppress(Exception):
                    match = re.fullmatch(r'mount:([0-9a-f]{40}):([0-9a-f]{64})', read_json(path).get('image', ''))
                    if match:
                        shas.add(match[1])
                        keys.add(match[2])
            now, keep = time.time(), self.c['cacheDays'] * 86400
            def old(path, seconds):
                return now - path.lstat().st_mtime > seconds
            sources = self.mount_root / 'src'
            for item in (sources.iterdir() if sources.is_dir() else []):
                name = item.name.removesuffix('.json')
                if item.name.startswith('.tmp-'):
                    if old(item, 3600):
                        shutil.rmtree(item, ignore_errors=True)
                elif name not in shas and name != self.request['sha'] and old(item, 6 * 3600):
                    if item.is_dir() and not item.is_symlink():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
            deps = self.mount_root / 'deps'
            for item in (deps.iterdir() if deps.is_dir() else []):
                if (item.name.startswith('.tmp-') and old(item, 3600)) or (item.name not in keys and old(item, keep)):
                    shutil.rmtree(item, ignore_errors=True)
            steps = self.mount_root / 'steps'
            for step in (steps.iterdir() if steps.is_dir() else []):
                for item in step.iterdir():
                    if old(item, 3600 if item.name.endswith('.tmp') else keep):
                        shutil.rmtree(item, ignore_errors=True)
        except Exception:
            pass

    def owned_dir(self, path):
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(not path.is_symlink(), 'data directory must not be a symlink')
        os.chmod(path, 0o700)
        if os.geteuid() == 0:
            os.chown(path, self.c['uid'], self.c['gid'])
        else:
            require(path.stat().st_uid == self.c['uid'], 'data directory needs configured service ownership')
        return path

    def sandbox(self):
        return ['--read-only', '--user', '%s:%s' % (self.c['uid'], self.c['gid']),
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                '--pids-limit', str(self.c['pids']), '--cpus', str(self.c['cpus']), '--memory', self.c['memory'],
                '--memory-swap', self.c['memory'], '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m',
                '--log-driver', 'local', '--log-opt', 'max-size=5m', '--log-opt', 'max-file=2',
                '--env', 'PREVIEW_ROOT=/preview']

    @staticmethod
    def mount(path, target, readonly=False):
        path = Path(path)
        require(',' not in str(path) and path.is_dir() and path.resolve() == path.absolute(), 'invalid or symlinked mount path')
        return ['--mount', 'type=bind,source=%s,target=%s%s' % (path, target, ',readonly' if readonly else '')]

    def helper(self, image, command, data=None, source=None, readonly=False, source_sidecars=False):
        args = ['run', '--rm', '--network', 'none', *self.sandbox()]
        if data is not None:
            args += self.mount(data, '/preview', readonly)
        if source is not None:
            # Only an already trusted previous image may create SQLite sidecars for a private backup.
            # The configured shared synthetic source always remains a fully read-only mount.
            args += self.mount(source, '/source', not source_sidecars)
        mounts, reference = self.runtime(image)
        args += [*mounts, '--entrypoint', self.c['helperCommand'][0], reference, *self.c['helperCommand'][1:], command]
        if command == 'copy':
            args += ['--from', '/source', '--to', '/preview']
        return self.docker(*args, json_output=(command == 'schema'))

    def shared_source(self, descriptor):
        require(self.c.get('syntheticRoot'), 'syntheticRoot is not configured')
        source = Path(self.c['syntheticRoot'])
        require(source.is_absolute() and source.is_dir() and not source.is_symlink(), 'invalid synthetic source')
        require((source / self.c['provenanceFile']).is_file(), 'synthetic source marker missing')
        require(self.c.get('devBaseline') and Path(self.c['devBaseline']).is_absolute(), 'devBaseline is not configured')
        baseline = read_json(self.c['devBaseline'])
        current = read_json(source / self.c['descriptorFile'])
        require(baseline == current, 'development baseline changed')
        for field in self.c['descriptorFields']:
            require(field in descriptor and descriptor[field] == baseline.get(field), 'development schema mismatch')
        return source

    def stop_app(self):
        name = self.resource_name
        # Inspect before removing; avoid ignoring daemon or permissions failures.
        existing = self.docker('ps', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.Names}}')
        if existing:
            self.docker('stop', '--time', '20', name)
            self.docker('rm', name)

    def remove_network(self):
        name = self.resource_name
        networks = self.docker('network', 'ls', '--filter', 'name=^' + name + '$', '--format', '{{.Name}}')
        if networks:
            self.docker('network', 'rm', name)

    def archive_slot(self, data):
        if data.exists():
            archives = self.pr_root / 'archives'
            archives.mkdir(mode=0o700, exist_ok=True)
            target = archives / (data.parent.name + '-' + str(int(time.time())) + '-' + uuid.uuid4().hex)
            os.rename(data, target)
            return str(target.relative_to(self.root))
        return None

    def prepare_data(self, image, descriptor, mode):
        if mode == 'dev-shared':
            source = self.shared_source(descriptor)
            self.helper(image, 'check', source, readonly=True)
            return source
        slot = self.pr_root / 'slots' / mode
        data = slot / 'data'
        slot.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata_path = slot / 'image.json'
        if self.request['action'] in ('reset-db', 'refresh-dev-copy'):
            archive = self.archive_slot(data)
            self.event('archived', archive=archive)
        if not data.exists():
            self.owned_dir(data)
            if mode == 'dev-copy':
                source = self.shared_source(descriptor)
                self.helper(image, 'check', source, readonly=True)
                self.helper(image, 'copy', data, source)
            else:
                self.helper(image, 'init', data)
        else:
            require(metadata_path.is_file(), 'existing data has no trusted image metadata')
            previous_image = read_json(metadata_path)['image']
            backup = self.pr_root / 'backups' / (mode + '-' + str(int(time.time())) + '-' + uuid.uuid4().hex)
            self.owned_dir(backup)
            self.helper(previous_image, 'copy', backup, data, source_sidecars=True)
            self.event('backed-up', backup=str(backup.relative_to(self.root)))
            self.helper(image, 'migrate', data)
        # init/migrate check the candidate before closing its writable connection; a second cold
        # read-only mount can fail solely because SQLite needs transient WAL sidecars.
        atomic_json(metadata_path, {'image': image, 'sha': self.request['sha']})
        return data

    def start_app(self, image, data, mode):
        name = self.resource_name
        mounts, reference = self.runtime(image)
        self.remove_network()
        self.docker('network', 'create', '--internal', '--driver', 'bridge', '--label', 'preview-server.managed=true', name)
        origins = self.urls()
        environment = {**self.c['runtimeEnv'], 'PREVIEW_SHA': self.request['sha'],
                       'PREVIEW_DATABASE_MODE': mode, 'PREVIEW_PORT': str(self.c['port'])}
        for endpoint in self.c['endpoints']:
            if endpoint.get('originEnv'):
                environment[endpoint['originEnv']] = origins[endpoint['name']]
        env_args = [arg for key, value in environment.items() for arg in ('--env', key + '=' + value)]
        args = ['run', '-d', '--name', name, '--restart', 'unless-stopped', '--network', name, *self.sandbox(),
                '--label', 'preview-server.managed=true', '--label', 'preview-server.sha=' + self.request['sha'],
                *self.mount(data, '/preview'),
                *self.mount(data / self.c['secretsDirectory'], '/preview/' + self.c['secretsDirectory'], True),
                *env_args, *mounts, '--entrypoint', self.c['serveCommand'][0], reference, *self.c['serveCommand'][1:]]
        self.docker(*args)
        details = self.docker('inspect', name, json_output=True)[0]
        expected = self.docker('image', 'inspect', reference, json_output=True)[0]['Id']
        require(details['Image'] == expected, 'running image does not match candidate')
        # Docker 29 does not publish ports on --internal bridges. The trusted host-network
        # proxy reaches the container's private address directly; no app port is published.
        network = self.docker('network', 'inspect', name, json_output=True)[0]
        require(network.get('Internal') is True and network.get('Driver') == 'bridge', 'invalid runtime network')
        attached = details['NetworkSettings']['Networks']
        require(set(attached) == {name}, 'unexpected runtime network attachment')
        ip = ipaddress.ip_address(attached[name]['IPAddress'])
        require(ip.version == 4 and ip.is_private and not ip.is_loopback, 'invalid private runtime address')
        subnets = [ipaddress.ip_network(item['Subnet']) for item in network.get('IPAM', {}).get('Config', []) if item.get('Subnet')]
        require(any(ip in subnet for subnet in subnets), 'runtime address is outside its network')
        require(not any(details['NetworkSettings'].get('Ports', {}).values()), 'runtime ports must not be published')
        upstream = '%s:%s' % (ip, self.c['port'])
        host = next(iter(self.urls().values())).removeprefix('https://')
        self.wait_health('http://%s%s' % (upstream, self.c['healthPath']), host=host)
        return upstream

    def wait_health(self, url, sha_header=False, host=None):
        deadline = time.monotonic() + self.c['healthTimeout']
        request = urllib.request.Request(url, headers={'Host': host} if host else {})
        while True:
            try:
                with self.opener.open(request, timeout=5) as response:
                    good = response.status == 200 and response.headers.get(self.c['revisionHeader']) == self.request['sha']
                    if sha_header:
                        good = good and response.headers.get('X-Preview-Sha') == self.request['sha']
                    if good:
                        return
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise PreviewError('preview health check failed')
            time.sleep(1)

    def handle(self, request):
        mode = validate_request(request, self.c['repository'])
        self.token = request.get('githubToken')
        request = {key: value for key, value in request.items() if key != 'githubToken'}
        require(socket.gethostname() not in self.c['forbiddenHosts'] and not Path(self.c['controlHostMarker']).exists(),
                'builds are forbidden on the control host')
        self.request, self.pr = request, request['prNumber']
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        (self.root / 'home').mkdir(mode=0o700, exist_ok=True)
        locks = self.root / 'locks'
        locks.mkdir(mode=0o700, exist_ok=True)
        if request['action'] == 'warm':
            require(self.c.get('buildMode') == 'mount', 'warm needs mount mode')
            with lock(locks / 'global.lock'):
                self.fetch_base()
                started = time.monotonic()
                self.build()
                self.collect()
            return dict(state='warmed', sha=request['sha'], seconds=round(time.monotonic() - started, 1), build=self.build_summary)
        self.pr_root.mkdir(mode=0o700, exist_ok=True)
        with lock(locks / ('pr-%s.lock' % self.pr)), lock(locks / 'global.lock'):
            receipt_path = self.pr_root / 'receipt.json'
            self.receipt = read_json(receipt_path) if receipt_path.exists() else {}
            disruptive = False
            cleanup_required = False
            try:
                self.github_current(stopping=request['action'] == 'stop')
                if request['action'] == 'stop':
                    mode = self.receipt.get('databaseMode', 'isolated')
                    failed = False
                    for cleanup in (self.update_route, self.stop_app, self.remove_network):
                        try:
                            cleanup()
                        except Exception:
                            failed = True
                    if failed:
                        cleanup_required = True
                        raise PreviewError('preview stop needs cleanup; inspect private receipt')
                    return self.event('stopped', databaseMode=mode, cleanupRequired=False)
                self.fetch_current()
                self.event('building', databaseMode=mode)
                image = self.build()
                descriptor = self.helper(image, 'schema')
                # Validate shared/copy source before any existing web process is stopped.
                if mode == 'dev-shared' or (mode == 'dev-copy' and
                        (request['action'] in ('refresh-dev-copy', 'reset-db') or not (self.pr_root / 'slots/dev-copy/data').exists())):
                    self.shared_source(descriptor)
                self.github_current()
                self.fetch_current()
                evicted = self.check_capacity()
                disruptive = True
                self.update_route()
                self.stop_app()
                self.event('preparing-data', image=image, build=self.build_summary)
                data = self.prepare_data(image, descriptor, mode)
                upstream = self.start_app(image, data, mode)
                self.github_current()
                self.fetch_current()
                self.update_route(upstream)
                urls = self.urls()
                for url in urls.values():
                    self.wait_health(url + self.c['healthPath'], sha_header=True)
                result = self.event('ready', image=image, upstream=upstream, databaseMode=mode, urls=urls, evictedPreviews=evicted)
                self.collect()
                return result
            except Exception:
                if disruptive:
                    # Try every cleanup even if Caddy is unavailable. Never restart an old image
                    # after migration; data and backups survive.
                    for cleanup in (self.update_route, self.stop_app, self.remove_network):
                        try:
                            cleanup()
                        except Exception:
                            cleanup_required = True
                self.event('failed', databaseMode=mode, cleanupRequired=cleanup_required)
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('command', choices=['request'])
    args = parser.parse_args()
    os.umask(0o077)
    def interrupted(signum, frame):
        raise PreviewError('request interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        config = config_from(args.config)
        raw = sys.stdin.buffer.read(65537)
        require(len(raw) <= 65536, 'request is too large')
        request = json.loads(raw)
        result = Manager(config).handle(request)
        print(json.dumps(result, sort_keys=True))
        return 0
    except PreviewError as error:
        print(str(error), file=sys.stderr)
    except Exception:
        print('preview request failed; inspect the private receipt and journal', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
