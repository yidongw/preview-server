"""Run with python3 -m unittest discover -s docker -p 'test_*.py'. No Docker/host access."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('preview', Path(__file__).with_name('preview.py'))
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)
SHA = 'a' * 40
OTHER = 'b' * 40
REPO = 'example/widgets'
DESCRIPTOR = dict(formatVersion=1, sourceHash='source', migrations=['001'], schemaHash='schema')


def request(action='deploy', labels=None):
    return dict(repository=REPO, prNumber=42, sha=SHA, action=action, labels=labels or [])


class ManagerTests(unittest.TestCase):
    def setUp(self):
        hostname = patch.object(preview.socket, 'gethostname', return_value='oracle-preview')
        hostname.start()
        self.addCleanup(hostname.stop)
        disk = patch.object(preview.shutil, 'disk_usage', return_value=Mock(free=100 * 1024**3))
        disk.start()
        self.addCleanup(disk.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        config_path = self.root / 'config.json'
        config_path.write_text(json.dumps(dict(root=str(self.root), sourceRepo=str(self.root / 'bare'),
                                               uid=os.getuid() or 1000, gid=os.getgid() or 1000,
                                               dockerDataRoot=str(self.root), repository=REPO, project='widgets', dockerfile='Dockerfile',
                                               helperCommand=['python3', '/app/preview.py'], serveCommand=['python3', '/app/preview.py', 'serve'],
                                               endpoints=[dict(name='web', domain='preview.example.com', originEnv='APP_ORIGIN'), dict(name='admin', domain='preview-admin.example.com', originEnv='ADMIN_ORIGIN')],
                                               forbiddenHosts=['control-host'])))
        self.config = preview.config_from(config_path)
        self.manager = preview.Manager(self.config)
        self.manager.request, self.manager.pr = request(), 42
        self.manager.pr_root.mkdir()
        self.manager.receipt = {}

    def mock_lifecycle(self, mode='isolated', action='deploy'):
        manager = self.manager
        req = request(action, [] if mode == 'isolated' else ['preview:db-' + mode])
        manager.github_current = Mock()
        manager.fetch_current = Mock()
        manager.check_capacity = Mock(return_value=[])
        manager.build = Mock(return_value='candidate')
        manager.helper = Mock(return_value=DESCRIPTOR)
        manager.shared_source = Mock(return_value=self.root / 'synthetic')
        manager.update_route = Mock()
        manager.stop_app = Mock()
        manager.remove_network = Mock()
        manager.start_app = Mock(return_value=49152)
        manager.wait_health = Mock()
        return req

    def test_strict_request_and_no_host_commands(self):
        for changes in ({'prNumber': True}, {'sha': '../../bad'}, {'repository': 'evil/repo'},
                        {'command': 'id'}, {'path': '/etc'}, {'action': 'shell'}, {'labels': 'label'}):
            with self.subTest(changes=changes), self.assertRaises(preview.PreviewError):
                preview.validate_request({**request(), **changes}, REPO)
        self.assertEqual(preview.validate_request(request(), REPO), 'isolated')

    def test_ephemeral_credential_accepts_token_punctuation_but_no_controls(self):
        for token in ('ghs_' + 'x' * 40, 'eyJ.test-token_payload.signature+/='):
            self.assertEqual(preview.validate_request({**request(), 'githubToken': token}, REPO), 'isolated')
        for token in ('x' * 30 + '\r\nInjected: value', 'x' * 30 + '\x00', 'x' * 30 + ' ', 'short'):
            with self.subTest(token_length=len(token)), self.assertRaises(preview.PreviewError):
                preview.validate_request({**request(), 'githubToken': token}, REPO)

    def test_database_conflict_and_reset_shared_exclusion(self):
        with self.assertRaisesRegex(preview.PreviewError, 'conflicting'):
            preview.validate_request(request(labels=['preview:db-isolated', 'preview:db-dev-copy']), REPO)
        with self.assertRaisesRegex(preview.PreviewError, 'cannot reset'):
            preview.validate_request(request('reset-db', ['preview:db-dev-shared']), REPO)
        with self.assertRaisesRegex(preview.PreviewError, 'refresh requires'):
            preview.validate_request(request('refresh-dev-copy'), REPO)

    def github(self, **changes):
        value = dict(state='open', head=dict(sha=SHA, repo=dict(full_name=REPO)),
                     base=dict(ref='main', repo=dict(full_name=REPO)), labels=[])
        value.update(changes)
        self.manager.http = Mock(return_value=value)
        return value

    def test_live_github_rejects_stale_closed_fork_and_label_changes(self):
        self.github()
        self.manager.github_current()
        for changes in (dict(state='closed'), dict(head=dict(sha=OTHER, repo=dict(full_name=REPO))),
                        dict(head=dict(sha=SHA, repo=dict(full_name='fork/repo'))),
                        dict(base=dict(ref='feature', repo=dict(full_name=REPO))),
                        dict(labels=[dict(name='preview:db-dev-shared')])):
            with self.subTest(changes=changes):
                self.github(**changes)
                with self.assertRaises(preview.PreviewError):
                    self.manager.github_current()

    def test_stop_closed_or_label_removed_but_not_newer_open_head(self):
        self.github(state='closed')
        self.manager.github_current(stopping=True)
        self.github(labels=[dict(name='preview:db-dev-shared')])
        self.manager.github_current(stopping=True)
        self.github(head=dict(sha=OTHER))
        with self.assertRaisesRegex(preview.PreviewError, 'stale'):
            self.manager.github_current(stopping=True)

    def test_git_fetch_exact_pull_ref_and_stale_rejection(self):
        self.manager.run = Mock(side_effect=['', OTHER])
        with self.assertRaisesRegex(preview.PreviewError, 'stale'):
            self.manager.fetch_current()
        args = self.manager.run.call_args_list[0].args[0]
        self.assertIn('+refs/pull/42/head:refs/previews/widgets/pr-42', args)
        self.assertNotIn('checkout', args)

    def test_stale_before_build_leaves_old_application(self):
        req = self.mock_lifecycle()
        self.manager.fetch_current.side_effect = preview.PreviewError('stale PR SHA')
        with self.assertRaises(preview.PreviewError):
            self.manager.handle(req)
        self.manager.build.assert_not_called()
        self.manager.stop_app.assert_not_called()

    def test_stale_after_build_before_stop_preserves_old_application(self):
        req = self.mock_lifecycle()
        self.manager.github_current.side_effect = [None, preview.PreviewError('stale')]
        with self.assertRaises(preview.PreviewError):
            self.manager.handle(req)
        self.manager.build.assert_called_once()
        self.manager.check_capacity.assert_not_called()
        self.manager.stop_app.assert_not_called()

    def test_stale_after_start_never_publishes_route(self):
        req = self.mock_lifecycle()
        self.manager.prepare_data = Mock(return_value=self.root)
        self.manager.github_current.side_effect = [None, None, preview.PreviewError('stale')]
        with self.assertRaises(preview.PreviewError):
            self.manager.handle(req)
        self.assertTrue(all(not call.args for call in self.manager.update_route.call_args_list))
        self.assertEqual(read_json(self.manager.pr_root / 'receipt.json')['state'], 'failed')

    def test_build_before_stop_and_backup_before_migrate(self):
        req = self.mock_lifecycle()
        data = self.manager.pr_root / 'slots/isolated/data'
        data.mkdir(parents=True)
        (data / 'sentinel').write_text('keep')
        preview.atomic_json(data.parent / 'image.json', dict(image='previous', sha=OTHER))
        order = []
        self.manager.build.side_effect = lambda: order.append('build') or 'candidate'
        self.manager.stop_app.side_effect = lambda: order.append('stop')
        def helper(image, command, *args, **kwargs):
            order.append(command)
            if command == 'schema':
                return DESCRIPTOR
        self.manager.helper.side_effect = helper
        result = self.manager.handle(req)
        self.assertEqual(result['state'], 'ready')
        self.assertLess(order.index('build'), order.index('stop'))
        self.assertLess(order.index('stop'), order.index('copy'))
        self.assertLess(order.index('copy'), order.index('migrate'))
        self.assertEqual((data / 'sentinel').read_text(), 'keep')
        copies = [c for c in self.manager.helper.call_args_list if c.args[1] == 'copy']
        self.assertEqual(copies[0].args[0], 'previous')
        self.assertEqual(copies[0].args[3], data)

    def test_failed_migration_stops_and_does_not_roll_back(self):
        req = self.mock_lifecycle()
        self.manager.prepare_data = Mock(side_effect=preview.PreviewError('migration failed'))
        with self.assertRaises(preview.PreviewError):
            self.manager.handle(req)
        self.manager.start_app.assert_not_called()
        self.assertEqual(self.manager.stop_app.call_count, 2)
        self.assertEqual(read_json(self.manager.pr_root / 'receipt.json')['state'], 'failed')

    def test_mode_switch_retains_both_slots_and_push_reuses_data(self):
        self.manager.helper = Mock()
        first = self.manager.prepare_data('image1', DESCRIPTOR, 'isolated')
        (first / 'sentinel').write_text('isolated')
        source = self.root / 'synthetic'
        source.mkdir()
        self.manager.shared_source = Mock(return_value=source)
        second = self.manager.prepare_data('image1', DESCRIPTOR, 'dev-copy')
        (second / 'sentinel').write_text('copy')
        self.manager.prepare_data('image2', DESCRIPTOR, 'isolated')
        self.manager.prepare_data('image2', DESCRIPTOR, 'dev-copy')
        self.assertEqual((first / 'sentinel').read_text(), 'isolated')
        self.assertEqual((second / 'sentinel').read_text(), 'copy')
        self.assertEqual(self.manager.shared_source.call_count, 1)

    def test_reset_archives_previous_directory(self):
        self.manager.helper = Mock()
        data = self.manager.prepare_data('old', DESCRIPTOR, 'isolated')
        (data / 'sentinel').write_text('retain archived')
        self.manager.request['action'] = 'reset-db'
        self.manager.prepare_data('new', DESCRIPTOR, 'isolated')
        self.assertFalse((data / 'sentinel').exists())
        archived = list((self.manager.pr_root / 'archives').glob('isolated-*'))
        self.assertEqual(len(archived), 1)
        self.assertEqual((archived[0] / 'sentinel').read_text(), 'retain archived')

    def synthetic_source(self):
        source = self.root / 'synthetic'
        (source / 'secrets').mkdir(parents=True)
        preview.atomic_json(source / 'secrets/synthetic.json', dict(synthetic=True))
        preview.atomic_json(source / 'secrets/schema.json', DESCRIPTOR)
        baseline = self.root / 'baseline.json'
        preview.atomic_json(baseline, DESCRIPTOR)
        self.manager.c.update(syntheticRoot=str(source), devBaseline=str(baseline))
        return source

    def test_shared_requires_source_marker_saved_baseline_and_full_schema(self):
        source = self.synthetic_source()
        self.assertEqual(self.manager.shared_source(DESCRIPTOR), source)
        for field in ('formatVersion', 'sourceHash', 'migrations', 'schemaHash'):
            with self.subTest(field=field), self.assertRaisesRegex(preview.PreviewError, 'mismatch'):
                self.manager.shared_source({**DESCRIPTOR, field: 'different'})
        preview.atomic_json(source / 'secrets/schema.json', {**DESCRIPTOR, 'migrations': []})
        with self.assertRaisesRegex(preview.PreviewError, 'baseline changed'):
            self.manager.shared_source(DESCRIPTOR)
        (source / 'secrets/synthetic.json').unlink()
        with self.assertRaisesRegex(preview.PreviewError, 'marker missing'):
            self.manager.shared_source(DESCRIPTOR)

    def test_shared_never_initializes_or_migrates(self):
        source = self.synthetic_source()
        self.manager.helper = Mock()
        self.assertEqual(self.manager.prepare_data('image', DESCRIPTOR, 'dev-shared'), source)
        self.manager.helper.assert_called_once_with('image', 'check', source, readonly=True)

    def test_stop_removes_app_route_but_retains_data(self):
        req = self.mock_lifecycle(action='stop')
        data = self.manager.pr_root / 'slots/isolated/data'
        data.mkdir(parents=True)
        (data / 'db').write_text('keep')
        result = self.manager.handle(req)
        self.assertEqual(result['state'], 'stopped')
        self.assertTrue((data / 'db').exists())
        self.manager.build.assert_not_called()
        self.manager.update_route.assert_called_once_with()

    def test_route_replacement_preserves_other_routes_and_sets_sha(self):
        other = dict(**{'@id': 'unrelated'}, handle=[])
        self.manager.http = Mock(side_effect=[[other, {'@id': 'preview-widgets-pr-42'}], None])
        self.manager.update_route('172.24.0.2:8080')
        kwargs = self.manager.http.call_args.kwargs
        self.assertEqual(kwargs['method'], 'PATCH')
        routes = kwargs['body']
        self.assertEqual(routes[1], other)
        route = routes[0]
        self.assertEqual(route['match'][0]['host'], ['pr-42.preview.example.com', 'pr-42.preview-admin.example.com'])
        self.assertEqual(route['handle'][0]['response']['set']['X-Preview-Sha'], [SHA])
        self.assertEqual(route['handle'][1]['upstreams'], [dict(dial='172.24.0.2:8080')])

    def test_capacity_counts_previews_and_updates_reuse_own_slot(self):
        self.manager.c['maxPreviews'] = 1
        self.manager.http = Mock(return_value=[{'@id': 'preview-widgets-pr-42'}, {'@id': 'unrelated'}])
        self.manager.check_capacity()
        self.manager.http.return_value = [{'@id': 'preview-widgets-pr-43'}]
        with self.assertRaisesRegex(preview.PreviewError, 'successful deployment record'):
            self.manager.check_capacity()

    def capacity_fixture(self):
        self.manager.c['maxPreviews'] = 2
        routes = [{'@id': 'preview-widgets-pr-43'}, {'@id': 'preview-widgets-pr-44'}, {'@id': 'unrelated'}]
        records = [dict(state='ready', prNumber=43, sha=SHA, time=10),
                   dict(state='ready', prNumber=44, sha=OTHER, time=20),
                   dict(state='ready', prNumber=43, sha=SHA, time=30),
                   dict(state='failed', prNumber=44, sha=SHA, time=40)]
        (self.root / 'journal.jsonl').write_text(''.join(json.dumps(record) + '\n' for record in records))
        for number in (43, 44):
            data = self.root / f'pr-{number}/slots/isolated/data'
            data.mkdir(parents=True)
            (data / 'sentinel').write_text('retained')
        def http(manager, url, method='GET', body=None, **kwargs):
            if method == 'PATCH':
                routes[:] = body
            return copy.deepcopy(routes)
        return routes, http

    def test_capacity_evicts_oldest_successful_deployment_preserves_data_and_returns_410(self):
        routes, http = self.capacity_fixture()
        with patch.object(preview.Manager, 'http', http), patch.object(preview.Manager, 'docker', return_value='container') as docker:
            self.assertEqual(self.manager.check_capacity(), [44])
        self.assertEqual([route['@id'] for route in routes], ['evicted-preview-widgets-pr-44', 'preview-widgets-pr-43', 'unrelated'])
        self.assertEqual(routes[0]['handle'][0]['status_code'], 410)
        self.assertIn('pr-44.preview.example.com', routes[0]['match'][0]['host'])
        receipt = read_json(self.root / 'pr-44/receipt.json')
        self.assertEqual((receipt['state'], receipt['evictedBy'], receipt['sha']), ('evicted', 42, OTHER))
        self.assertEqual((self.root / 'pr-44/slots/isolated/data/sentinel').read_text(), 'retained')
        self.assertTrue(all('preview-widgets-pr-43' not in call.args for call in docker.call_args_list))
        with patch.object(preview.Manager, 'http', http):
            self.manager.pr = 44
            self.manager.update_route('172.24.0.2:8080')
        self.assertEqual([route['@id'] for route in routes], ['preview-widgets-pr-44', 'preview-widgets-pr-43', 'unrelated'])

    def test_capacity_can_recover_when_limit_was_lowered_and_preserves_other_projects(self):
        routes, http = self.capacity_fixture()
        self.manager.c['maxPreviews'] = 1
        routes.insert(0, {'@id': 'preview-another-pr-1'})
        with patch.object(preview.Manager, 'http', http), patch.object(preview.Manager, 'docker', return_value=''):
            self.assertEqual(self.manager.check_capacity(), [44, 43])
        self.assertIn('preview-another-pr-1', [route['@id'] for route in routes])
        self.assertEqual(sum(route['@id'].startswith('evicted-') for route in routes), 2)

    def test_failed_candidate_or_stale_request_never_evicts(self):
        req = self.mock_lifecycle()
        self.manager.build.side_effect = preview.PreviewError('build failed')
        with self.assertRaises(preview.PreviewError):
            self.manager.handle(req)
        self.manager.check_capacity.assert_not_called()

    def test_eviction_cleanup_failure_blocks_new_deployment_and_attempts_all_cleanup(self):
        routes, http = self.capacity_fixture()
        with patch.object(preview.Manager, 'http', http), \
                patch.object(preview.Manager, 'stop_app', side_effect=preview.PreviewError('cannot stop')), \
                patch.object(preview.Manager, 'remove_network') as remove:
            with self.assertRaisesRegex(preview.PreviewError, 'eviction needs cleanup'):
                self.manager.check_capacity()
        remove.assert_called_once()
        receipt = read_json(self.root / 'pr-44/receipt.json')
        self.assertEqual(receipt['state'], 'eviction-failed')
        self.assertTrue(receipt['cleanupRequired'])
        self.assertNotIn('evicted-preview-widgets-pr-44', [route['@id'] for route in routes])

    def test_helper_constrained_networkless_and_does_not_forward_outputs(self):
        data = self.root / 'data'
        data.mkdir()
        self.manager.docker = Mock(return_value='')
        self.manager.helper('image', 'check', data, readonly=True)
        args = self.manager.docker.call_args.args
        self.assertIn('none', args)
        self.assertIn('--read-only', args)
        self.assertIn('ALL', args)
        self.assertIn('no-new-privileges:true', args)
        self.assertIn('--pids-limit', args)
        self.assertTrue(any('target=/preview,readonly' in arg for arg in args))
        self.assertNotIn('--privileged', args)
        self.assertNotIn('/var/run/docker.sock', str(args))

    def test_symlink_data_mount_refused(self):
        target = self.root / 'target'
        target.mkdir()
        symlink = self.root / 'link'
        symlink.symlink_to(target)
        with self.assertRaisesRegex(preview.PreviewError, 'mount'):
            self.manager.mount(symlink, '/preview')

    def test_command_failure_does_not_echo_credentials_or_source_output(self):
        with patch.object(preview.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['docker'], stderr=b'PASSWORD=secret')):
            with self.assertRaises(preview.PreviewError) as caught:
                self.manager.run(['docker', 'run'])
        self.assertNotIn('secret', str(caught.exception))

    def test_no_build_on_control_host(self):
        with patch.object(preview.socket, 'gethostname', return_value='control-host'):
            with self.assertRaisesRegex(preview.PreviewError, 'control host'):
                self.manager.handle(request())

    def test_builder_rejects_unbounded_privileged_or_wrong_endpoint(self):
        good_builder = json.dumps(dict(Name=self.config['builder'], Driver='remote', Nodes=[dict(Endpoint=self.config['builderEndpoint'])]))
        good_info = dict(Config=dict(User='1000'), HostConfig=dict(Memory=1024, CpuQuota=100000,
                                                                  CpuPeriod=100000, Privileged=False))
        for changes in (dict(Privileged=True), dict(Memory=0), dict(CpuQuota=0)):
            info = copy.deepcopy(good_info)
            info['HostConfig'].update(changes)
            self.manager.docker = Mock(side_effect=[good_builder, [info]])
            self.manager.build_context = Mock()
            with self.subTest(changes=changes), self.assertRaises(preview.PreviewError):
                self.manager.build()
            self.manager.build_context.assert_not_called()
        self.manager.docker = Mock(return_value=json.dumps(dict(Name=self.config['builder'], Driver='docker-container', Nodes=[])))
        with self.assertRaisesRegex(preview.PreviewError, 'endpoint'):
            self.manager.build()

    def test_shared_reset_refused_before_any_mutation(self):
        self.manager.github_current = Mock()
        self.manager.stop_app = Mock()
        with self.assertRaisesRegex(preview.PreviewError, 'cannot reset'):
            self.manager.handle(request('reset-db', ['preview:db-dev-shared']))
        self.manager.github_current.assert_not_called()
        self.manager.stop_app.assert_not_called()

    def test_stop_ignores_conflicting_labels_and_uses_previous_mode(self):
        req = self.mock_lifecycle(action='stop')
        req['labels'] = ['preview:db-isolated', 'preview:db-dev-shared']
        preview.atomic_json(self.manager.pr_root / 'receipt.json', dict(databaseMode='dev-copy'))
        self.assertEqual(self.manager.handle(req)['databaseMode'], 'dev-copy')
        self.manager.stop_app.assert_called_once()

    def test_builder_rejects_root_user_with_group(self):
        good_builder = json.dumps(dict(Name=self.config['builder'], Driver='remote', Nodes=[dict(Endpoint=self.config['builderEndpoint'])]))
        for user in ('0:0', '0:1000', 'root:1000', '1000:0', 'node', ''):
            info = dict(Config=dict(User=user), HostConfig=dict(Memory=1024, CpuQuota=100000,
                                                              CpuPeriod=100000, Privileged=False))
            self.manager.docker = Mock(side_effect=[good_builder, [info]])
            with self.subTest(user=user), self.assertRaisesRegex(preview.PreviewError, 'non-root'):
                self.manager.build()

    def test_dev_copy_reset_source_guard_runs_before_stopping_old_web(self):
        req = self.mock_lifecycle('dev-copy', 'reset-db')
        (self.manager.pr_root / 'slots/dev-copy/data').mkdir(parents=True)
        self.manager.shared_source.side_effect = preview.PreviewError('schema mismatch')
        with self.assertRaisesRegex(preview.PreviewError, 'schema mismatch'):
            self.manager.handle(req)
        self.manager.stop_app.assert_not_called()

    def test_cleanup_stops_container_even_when_caddy_fails(self):
        req = self.mock_lifecycle()
        self.manager.prepare_data = Mock(side_effect=preview.PreviewError('migrate failed'))
        self.manager.update_route.side_effect = [None, preview.PreviewError('Caddy unavailable')]
        with self.assertRaisesRegex(preview.PreviewError, 'migrate failed'):
            self.manager.handle(req)
        self.assertEqual(self.manager.stop_app.call_count, 2)
        self.manager.remove_network.assert_called_once()
        receipt = read_json(self.manager.pr_root / 'receipt.json')
        self.assertTrue(receipt['cleanupRequired'])

    def test_internal_health_sends_configured_host_header(self):
        response = Mock(status=200, headers={'X-Preview-App-Sha': SHA})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        self.manager.opener.open = Mock(return_value=response)
        self.manager.wait_health('http://172.24.0.2:8080/api/health', host='pr-42.preview.example.com')
        sent = self.manager.opener.open.call_args.args[0]
        self.assertEqual(sent.get_header('Host'), 'pr-42.preview.example.com')

    def test_public_health_requires_app_sha_and_route_sha(self):
        response = Mock(status=200, headers={'X-Preview-App-Sha': SHA, 'X-Preview-Sha': SHA})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        self.manager.opener.open = Mock(return_value=response)
        self.manager.wait_health('https://pr-42.preview.example.com/api/health', sha_header=True)
        for missing in ('X-Preview-App-Sha', 'X-Preview-Sha'):
            with self.subTest(missing=missing):
                response.headers = {'X-Preview-App-Sha': SHA, 'X-Preview-Sha': SHA}
                response.headers[missing] = OTHER
                with patch.object(preview.time, 'monotonic', side_effect=[0, 999]):
                    with self.assertRaisesRegex(preview.PreviewError, 'health'):
                        self.manager.wait_health('https://pr-42.preview.example.com/api/health', sha_header=True)

    def test_web_sandbox_internal_network_secret_mount_and_identity(self):
        data = self.root / 'runtime-data'
        (data / 'secrets').mkdir(parents=True)
        expected_image = dict(Id='sha256:expected')
        details = dict(Image='sha256:expected', NetworkSettings=dict(Ports={'8080/tcp': None},
                       Networks={'preview-widgets-pr-42': {'IPAddress': '172.24.0.2'}}))
        network = dict(Internal=True, Driver='bridge', IPAM=dict(Config=[dict(Subnet='172.24.0.0/16')]))
        self.manager.remove_network = Mock()
        self.manager.wait_health = Mock()
        self.manager.docker = Mock(side_effect=['network-id', 'container-id', [details], [expected_image], [network]])
        self.assertEqual(self.manager.start_app('candidate', data, 'isolated'), '172.24.0.2:8080')
        network_args = self.manager.docker.call_args_list[0].args
        run_args = self.manager.docker.call_args_list[1].args
        self.assertIn('--internal', network_args)
        self.assertNotIn('--publish', run_args)
        self.assertEqual(run_args[run_args.index('--restart') + 1], 'unless-stopped')
        self.assertIn('PREVIEW_SHA=' + SHA, run_args)
        self.assertTrue(any('target=/preview/secrets,readonly' in arg for arg in run_args))
        self.assertIn('no-new-privileges:true', run_args)
        self.assertEqual(run_args[-2:], ('/app/preview.py', 'serve'))
        self.assertIn('APP_ORIGIN=https://pr-42.preview.example.com', run_args)
        self.assertIn('ADMIN_ORIGIN=https://pr-42.preview-admin.example.com', run_args)
        self.assertIn('PREVIEW_PORT=8080', run_args)
        self.manager.wait_health.assert_called_once_with('http://172.24.0.2:8080/api/health', host='pr-42.preview.example.com')

    def test_buildx_ls_selects_exact_named_builder_from_json_lines(self):
        builder = dict(Name=self.config['builder'], Driver='remote', Nodes=[dict(Endpoint=self.config['builderEndpoint'])])
        info = dict(Config=dict(User='1000:1000'), HostConfig=dict(Memory=1024, CpuQuota=100000, CpuPeriod=100000, Privileged=False))
        self.manager.docker = Mock(side_effect=[json.dumps(dict(Name='default', Driver='docker')) + '\n' + json.dumps(builder) + '\n' + json.dumps(builder), [info]])
        self.manager.build_context = Mock()
        self.manager.build()
        self.assertEqual(self.manager.docker.call_args_list[0].args, ('buildx', 'ls', '--format', '{{json .}}'))

    def test_runtime_rejects_noninternal_network_extra_attachment_and_published_port(self):
        data = self.root / 'runtime-data'
        (data / 'secrets').mkdir(parents=True)
        good = dict(Image='image-id', NetworkSettings=dict(Ports={'8080/tcp': None}, Networks={'preview-widgets-pr-42': {'IPAddress': '172.24.0.2'}}))
        network = dict(Internal=True, Driver='bridge', IPAM=dict(Config=[dict(Subnet='172.24.0.0/16')]))
        for kind in ('external', 'extra-network', 'public-ip', 'outside-subnet', 'published'):
            details, net = copy.deepcopy(good), copy.deepcopy(network)
            if kind == 'external': net['Internal'] = False
            if kind == 'extra-network': details['NetworkSettings']['Networks']['bridge'] = {'IPAddress': '172.17.0.2'}
            if kind == 'public-ip': details['NetworkSettings']['Networks']['preview-widgets-pr-42']['IPAddress'] = '8.8.8.8'
            if kind == 'outside-subnet': details['NetworkSettings']['Networks']['preview-widgets-pr-42']['IPAddress'] = '172.25.0.2'
            if kind == 'published': details['NetworkSettings']['Ports']['8080/tcp'] = [dict(HostIp='0.0.0.0', HostPort='8080')]
            self.manager.remove_network = Mock()
            self.manager.docker = Mock(side_effect=['network', 'container', [details], [dict(Id='image-id')], [net]])
            with self.subTest(kind=kind), self.assertRaises(preview.PreviewError):
                self.manager.start_app('candidate', data, 'isolated')

    def test_project_namespace_and_named_endpoints_are_configured(self):
        self.assertEqual(self.manager.resource_name, 'preview-widgets-pr-42')
        self.manager.c['project'] = 'reports'
        self.assertEqual(self.manager.resource_name, 'preview-reports-pr-42')
        self.manager.c['endpoints'] = [dict(name='dashboard', domain='reports.example.net')]
        self.assertEqual(self.manager.urls(), {'dashboard': 'https://pr-42.reports.example.net'})
        self.assertEqual(self.manager.routes('172.24.0.2:8080')['match'][0]['host'], ['pr-42.reports.example.net'])

    def test_configuration_requires_repository_adapter_and_rejects_unsafe_paths(self):
        for field in ('repository', 'project', 'endpoints', 'dockerfile', 'helperCommand', 'serveCommand'):
            value = dict(self.config)
            del value[field]
            target = self.root / 'bad.json'
            target.write_text(json.dumps(value))
            with self.subTest(missing=field), self.assertRaises(preview.PreviewError):
                preview.config_from(target)
        for change in ({'dockerfile': '../Dockerfile'}, {'helperCommand': 'sh -c bad'},
                       {'project': '../other'}, {'repository': 'other;command'},
                       {'endpoints': [dict(name='web', domain='bad/domain')]}):
            target = self.root / 'bad.json'
            target.write_text(json.dumps({**self.config, **change}))
            with self.subTest(change=change), self.assertRaises(preview.PreviewError):
                preview.config_from(target)

    def test_ephemeral_token_only_reaches_github_and_fetch_not_docker_or_receipts(self):
        token = 'github_pat_' + 'x' * 40
        req = self.mock_lifecycle()
        req['githubToken'] = token
        self.manager.prepare_data = Mock(return_value=self.root)
        result = self.manager.handle(req)
        self.assertEqual(self.manager.token, token)
        self.assertNotIn('githubToken', self.manager.request)
        self.assertNotIn(token, json.dumps(result))
        self.assertNotIn(token, (self.root / 'journal.jsonl').read_text())
        response = Mock(stdout=b'ok')
        with patch.object(preview.subprocess, 'run', return_value=response) as run:
            self.manager.run(['git', 'fetch'], github_auth=True)
            self.assertIn('GIT_CONFIG_VALUE_0', run.call_args.kwargs['env'])
            self.manager.run(['docker', 'run'])
            self.assertNotIn('GIT_CONFIG_VALUE_0', run.call_args.kwargs['env'])
        self.manager.http = Mock(return_value=dict(state='open', head=dict(sha=SHA, repo=dict(full_name=REPO)), base=dict(ref='main', repo=dict(full_name=REPO)), labels=[]))
        preview.Manager.github_current(self.manager)
        self.assertEqual(self.manager.http.call_args.kwargs['headers']['Authorization'], 'Bearer ' + token)

    def test_durable_receipt_journal_no_secret_fields(self):
        self.manager.event('ready', port=49152)
        self.assertEqual(read_json(self.manager.pr_root / 'receipt.json')['sha'], SHA)
        journal = (self.root / 'journal.jsonl').read_text().splitlines()
        self.assertEqual(json.loads(journal[0])['state'], 'ready')
        self.assertEqual((self.root / 'journal.jsonl').stat().st_mode & 0o777, 0o600)


MOUNT = dict(buildMode='mount', baseImage='node:22.19.0-bookworm', workdir='/app',
             install=dict(command=['npm', 'ci'], keyFiles=['package.json', 'package-lock.json', 'apps/web/package.json'],
                          tmpfs=['.vite-temp'], env=dict(npm_config_cache='/cache')),
             steps=[dict(name='web', command=['npm', 'run', 'build', '-w', 'web'], inputs=['apps/web/', 'package'], outputs=['apps/web/dist']),
                    dict(name='server', command=['npm', 'run', 'server:build'], inputs=[''], exclude=['apps/', 'docs/'], outputs=['dist'])])
TREE = {'package.json': '100644 blob 1', 'package-lock.json': '100644 blob 2', 'apps/web/package.json': '100644 blob 3',
        'apps/web/src/main.ts': '100644 blob 4', 'server/server.ts': '100644 blob 5', 'docs/a.md': '100644 blob 6'}


class MountModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        path = self.root / 'config.json'
        path.write_text(json.dumps(dict(root=str(self.root), sourceRepo=str(self.root / 'bare'), uid=os.getuid() or 1000,
                                        gid=os.getgid() or 1000, dockerDataRoot=str(self.root), repository=REPO, project='widgets',
                                        dockerfile='Dockerfile', helperCommand=['node', 'adapter.cjs'], serveCommand=['node', 'adapter.cjs', 'serve'],
                                        endpoints=[dict(name='web', domain='preview.example.com')], **copy.deepcopy(MOUNT))))
        self.config = preview.config_from(path)
        self.manager = preview.Manager(self.config)
        self.manager.request, self.manager.pr = request(), 42
        self.tree = dict(TREE)
        self.manager.tree = Mock(side_effect=lambda sha: dict(self.tree))
        self.manager.extract = Mock(side_effect=self.extract)
        self.installs, self.restored, self.built = 0, [], []
        self.manager.docker = Mock(side_effect=self.docker)
        popen = patch.object(preview.subprocess, 'Popen', side_effect=self.popen)
        popen.start()
        self.addCleanup(popen.stop)

    def extract(self, sha, target, paths=None):
        (Path(target) / 'apps/web').mkdir(parents=True, exist_ok=True)
        (Path(target) / 'package.json').write_text('{}')

    @staticmethod
    def host(arg, mounts):
        for target, source in mounts.items():
            if arg == target or arg.startswith(target + '/'):
                return source + arg[len(target):]
        return arg

    def mounts(self, args):
        return {part.split('target=')[1].split(',')[0]: part.split('source=')[1].split(',')[0]
                for part in args if isinstance(part, str) and part.startswith('type=bind')}

    def docker(self, *args, **kwargs):
        mounts = self.mounts(args)
        if args[-1] == 'ci':
            self.installs += 1
            self.install_args = args
            work = Path(mounts['/work'])
            for module in ('node_modules', 'apps/web/node_modules'):
                (work / module).mkdir(parents=True)
            return ''
        script = args[-1]
        if 'cp -a /cache/' in script and 'mv ' not in script:
            self.restored.append(script)
            return ''
        for command in script.split(' && '):
            words = command.split()
            if words[0] == 'mkdir':
                Path(self.host(words[-1], mounts)).mkdir(parents=True, exist_ok=True)
            elif words[0] == 'mv':
                os.rename(self.host(words[1], mounts), self.host(words[2], mounts))
            elif words[0] == 'rm':
                pass
        return ''

    def popen(self, args, **kwargs):
        self.built.append(args[args.index('--entrypoint') + 2:])
        self.build_args = args
        process = Mock()
        process.wait.return_value = 0
        process.poll.return_value = 0
        return process

    def test_configuration_rejects_overlapping_outputs_and_missing_install(self):
        bad = copy.deepcopy(self.config)
        bad['steps'][1]['outputs'] = ['apps/web']
        with self.assertRaisesRegex(preview.PreviewError, 'overlap'):
            preview.mount_config(bad)
        bad = copy.deepcopy(self.config)
        del bad['install']
        with self.assertRaisesRegex(preview.PreviewError, 'install'):
            preview.mount_config(bad)
        bad = copy.deepcopy(self.config)
        bad['steps'][0]['outputs'] = ['../escape']
        with self.assertRaisesRegex(preview.PreviewError, 'outputs'):
            preview.mount_config(bad)

    def test_first_build_installs_with_network_and_builds_steps_without(self):
        handle = self.manager.build_mount()
        self.assertRegex(handle, r'^mount:a{40}:[0-9a-f]{64}$')
        self.assertEqual(self.installs, 1)
        self.assertNotIn('--network', self.install_args)
        self.assertNotIn(str(self.root / 'mount/src'), ' '.join(self.install_args))
        self.assertEqual(sorted(command[-1] for command in self.built), ['server:build', 'web'])
        self.assertEqual(self.build_args[self.build_args.index('--network') + 1], 'none')
        self.assertTrue(any(arg.startswith('/app/node_modules/.vite-temp:') for arg in self.build_args))
        self.assertEqual(self.manager.build_summary['steps'], dict(web='built', server='built'))
        source = self.root / 'mount/src' / SHA
        self.assertTrue((source / 'apps/web/node_modules').is_dir())
        self.assertEqual(read_json(self.root / 'mount/src' / (SHA + '.json'))['deps'], handle.split(':')[2])

    def test_same_commit_is_reused_and_unrelated_change_rebuilds_only_its_step(self):
        first = self.manager.build_mount()
        self.built.clear()
        self.assertEqual(self.manager.build_mount(), first)
        self.assertEqual(self.built, [])
        self.manager.request = dict(request(), sha=OTHER)
        self.tree['server/server.ts'] = '100644 blob 50'
        self.manager.build_mount()
        self.assertEqual(self.installs, 1)
        self.assertEqual([command[-1] for command in self.built], ['server:build'])
        self.assertEqual(len(self.restored), 1)
        self.assertIn('/cache/web/', self.restored[0])
        self.assertEqual(self.manager.build_summary['steps'], dict(web='cached', server='built'))
        # A documentation change is outside every step's inputs: nothing is built at all.
        self.built.clear()
        self.manager.request = dict(request(), sha='c' * 40)
        self.tree['docs/a.md'] = '100644 blob 60'
        self.manager.build_mount()
        self.assertEqual(self.built, [])

    def test_lockfile_change_reinstalls_and_rebuilds(self):
        self.manager.build_mount()
        self.built.clear()
        self.manager.request = dict(request(), sha=OTHER)
        self.tree['package-lock.json'] = '100644 blob 20'
        self.manager.build_mount()
        self.assertEqual(self.installs, 2)
        self.assertEqual(len(self.built), 2)

    def test_committed_dependency_directory_is_refused(self):
        self.manager.extract = Mock(side_effect=lambda sha, target, paths=None: (Path(target) / 'node_modules').mkdir(parents=True))
        self.manager.ensure_deps = Mock(return_value=('f' * 64, ['node_modules'], False))
        with self.assertRaisesRegex(preview.PreviewError, 'dependency directory'):
            self.manager.build_mount()
        self.assertFalse(list((self.root / 'mount/src').glob('.tmp-*')))

    def test_failed_step_discards_tree_and_saves_nothing(self):
        def failing(args, **kwargs):
            process = Mock()
            process.wait.return_value = 1
            process.poll.return_value = 1
            return process
        preview.subprocess.Popen.side_effect = failing
        with self.assertRaisesRegex(preview.PreviewError, 'build step failed'):
            self.manager.build_mount()
        self.assertFalse((self.root / 'mount/src' / SHA).exists())
        self.assertFalse(list((self.root / 'mount/steps').glob('*/*/.complete')))

    def test_runtime_mounts_tree_and_dependencies_read_only_on_base_image(self):
        handle = self.manager.build_mount()
        mounts, reference = self.manager.runtime(handle)
        self.assertEqual(reference, 'node:22.19.0-bookworm')
        joined = ' '.join(mounts)
        self.assertIn('target=/app,readonly', joined)
        self.assertIn('target=/app/apps/web/node_modules,readonly', joined)
        self.assertNotIn('tmpfs', joined)
        self.assertEqual(self.manager.runtime('preview-widgets:pr-42-' + SHA), ([], 'preview-widgets:pr-42-' + SHA))

    def test_warm_request_validation(self):
        warm = dict(repository=REPO, prNumber=0, sha=SHA, action='warm', labels=[])
        self.assertIsNone(preview.validate_request(warm, REPO))
        for bad in (dict(warm, prNumber=1), dict(warm, labels=['preview:db-dev-shared'])):
            with self.assertRaises(preview.PreviewError):
                preview.validate_request(bad, REPO)
        with self.assertRaises(preview.PreviewError):
            preview.validate_request(dict(warm, action='deploy'), REPO)

    def test_collect_keeps_sources_a_slot_uses(self):
        used = self.manager.build_mount()
        self.manager.request = dict(request(), sha=OTHER)
        self.tree['server/server.ts'] = '100644 blob 50'
        self.manager.build_mount()
        slot = self.root / 'pr-42/slots/isolated'
        slot.mkdir(parents=True)
        preview.atomic_json(slot / 'image.json', dict(image=used, sha=SHA))
        self.manager.request = dict(request(), sha='c' * 40)
        stale = time.time() - 7 * 3600
        for item in (self.root / 'mount/src').iterdir():
            os.utime(item, (stale, stale), follow_symlinks=False)
        self.manager.collect()
        self.assertTrue((self.root / 'mount/src' / SHA).is_dir())
        self.assertFalse((self.root / 'mount/src' / OTHER).exists())
        self.assertFalse((self.root / 'mount/src' / (OTHER + '.json')).exists())
        self.assertTrue(list((self.root / 'mount/steps/server').iterdir()))


def read_json(path):
    return json.loads(path.read_text())


if __name__ == '__main__':
    unittest.main()
