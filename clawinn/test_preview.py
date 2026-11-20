"""Run with python3 -m unittest discover -s clawinn -p 'test_*.py'. No Docker/host access."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('preview', Path(__file__).with_name('preview.py'))
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)
SHA = 'a' * 40
OTHER = 'b' * 40
REPO = 'cyl19970726/TokenToEarn'
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
                                               dockerDataRoot=str(self.root))))
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
        manager.check_capacity = Mock()
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
        self.assertIn('+refs/pull/42/head:refs/clawinn/pr-42', args)
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
        self.manager.http = Mock(side_effect=[[other, {'@id': 'clawinn-pr-42'}], None])
        self.manager.update_route(49152)
        kwargs = self.manager.http.call_args.kwargs
        self.assertEqual(kwargs['method'], 'PUT')
        routes = kwargs['body']
        self.assertEqual(routes[1], other)
        route = routes[0]
        self.assertEqual(route['match'][0]['host'], ['pr-42.preview.clawinn.xyz', 'pr-42.preview-admin.clawinn.xyz'])
        self.assertEqual(route['handle'][0]['response']['set']['X-Clawinn-Preview-Sha'], [SHA])
        self.assertEqual(route['handle'][1]['upstreams'], [dict(dial='127.0.0.1:49152')])

    def test_capacity_counts_previews_and_updates_reuse_own_slot(self):
        self.manager.c['maxPreviews'] = 1
        self.manager.http = Mock(return_value=[{'@id': 'clawinn-pr-42'}, {'@id': 'unrelated'}])
        self.manager.check_capacity()
        self.manager.http.return_value = [{'@id': 'clawinn-pr-43'}]
        with self.assertRaisesRegex(preview.PreviewError, 'capacity'):
            self.manager.check_capacity()

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
        with patch.object(preview.socket, 'gethostname', return_value='cx43-grab'):
            with self.assertRaisesRegex(preview.PreviewError, 'control host'):
                self.manager.handle(request())

    def test_builder_rejects_unbounded_privileged_or_wrong_endpoint(self):
        good_builder = dict(Driver='remote', Nodes=[dict(Endpoint=self.config['builderEndpoint'])])
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
        self.manager.docker = Mock(return_value=dict(Driver='docker-container', Nodes=[]))
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
        good_builder = dict(Driver='remote', Nodes=[dict(Endpoint=self.config['builderEndpoint'])])
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

    def test_public_health_requires_app_sha_and_route_sha(self):
        response = Mock(status=200, headers={'X-Clawinn-App-Sha': SHA, 'X-Clawinn-Preview-Sha': SHA})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        self.manager.opener.open = Mock(return_value=response)
        self.manager.wait_health('https://pr-42.preview.clawinn.xyz/api/health', sha_header=True)
        for missing in ('X-Clawinn-App-Sha', 'X-Clawinn-Preview-Sha'):
            with self.subTest(missing=missing):
                response.headers = {'X-Clawinn-App-Sha': SHA, 'X-Clawinn-Preview-Sha': SHA}
                response.headers[missing] = OTHER
                with patch.object(preview.time, 'monotonic', side_effect=[0, 999]):
                    with self.assertRaisesRegex(preview.PreviewError, 'health'):
                        self.manager.wait_health('https://pr-42.preview.clawinn.xyz/api/health', sha_header=True)

    def test_web_sandbox_loopback_network_secret_mount_and_identity(self):
        data = self.root / 'runtime-data'
        (data / 'secrets').mkdir(parents=True)
        expected_image = dict(Id='sha256:expected')
        details = dict(Image='sha256:expected', NetworkSettings=dict(Ports={'8080/tcp': [dict(HostIp='127.0.0.1', HostPort='49152')]}))
        self.manager.remove_network = Mock()
        self.manager.wait_health = Mock()
        self.manager.docker = Mock(side_effect=['network-id', 'container-id', [details], [expected_image]])
        self.assertEqual(self.manager.start_app('candidate', data, 'isolated'), 49152)
        network_args = self.manager.docker.call_args_list[0].args
        run_args = self.manager.docker.call_args_list[1].args
        self.assertIn('--internal', network_args)
        self.assertIn('127.0.0.1::8080', run_args)
        self.assertIn('PREVIEW_SHA=' + SHA, run_args)
        self.assertTrue(any('target=/preview/secrets,readonly' in arg for arg in run_args))
        self.assertIn('no-new-privileges:true', run_args)
        self.assertEqual(run_args[-2:], ('deploy/preview/app.cjs', 'serve'))

    def test_durable_receipt_journal_no_secret_fields(self):
        self.manager.event('ready', port=49152)
        self.assertEqual(read_json(self.manager.pr_root / 'receipt.json')['sha'], SHA)
        journal = (self.root / 'journal.jsonl').read_text().splitlines()
        self.assertEqual(json.loads(journal[0])['state'], 'ready')
        self.assertEqual((self.root / 'journal.jsonl').stat().st_mode & 0o777, 0o600)


def read_json(path):
    return json.loads(path.read_text())


if __name__ == '__main__':
    unittest.main()
