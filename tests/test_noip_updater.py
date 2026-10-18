import json
from pathlib import Path
import tempfile
import urllib.error
import unittest
from unittest.mock import Mock, patch
import noip_updater as app


class UpdaterTests(unittest.TestCase):
    def test_persistence_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            backup = app._bak_name(path)
            app.atomic_save_json(path, backup, {'noip': {'myip': '8.8.8.8'}})
            app.atomic_save_json(path, backup, {'noip': {'myip': '1.1.1.1'}})
            self.assertEqual(json.loads(backup.read_text())['noip']['myip'], '8.8.8.8')
            path.write_text('{broken')
            self.assertEqual(app.load_state(path)['noip']['myip'], '8.8.8.8')

    def test_unchanged_ip_sends_no_request(self):
        opener = Mock()
        self.assertFalse(app.update_once({}, {'noip': {'myip': '8.8.8.8'}},
                                        'unused', Mock(get_external_ip=lambda: '8.8.8.8'), opener,
                                        app.UpdateSession({'noip': {'myip': '8.8.8.8'}})))
        opener.open.assert_not_called()

    def test_failure_does_not_advance_state(self):
        state = {'noip': {}}
        with patch.object(app, 'noip_update', side_effect=app.RetryLater), \
             patch.object(app, 'atomic_save_json') as save:
            with self.assertRaises(app.RetryLater):
                app.update_once({}, state, 'unused', Mock(get_external_ip=lambda: '8.8.8.8'), Mock(), app.UpdateSession(state))
            self.assertEqual(state, {'noip': {}})
            save.assert_not_called()

    def test_protocol_responses(self):
        config = {'noip': {'hostname': 'example.ddns.net', 'useragent': 'test/1 user@example.com'}}
        for body, error in [(b'good 8.8.8.8', None), (b'nochg 8.8.8.8', None),
                            (b'badauth', app.PermanentUpdateError), (b'911', app.RetryLater),
                            (b'good', app.PermanentUpdateError),
                            (b'nochg 1.1.1.1', app.PermanentUpdateError),
                            (b'good 0.0.0.0', app.PermanentUpdateError),
                            (b'good invalid', app.PermanentUpdateError),
                            (b'good 8.8.8.8 extra', app.PermanentUpdateError),
                            (b'good 8.8.8.8\n911', app.RetryLater),
                            (b'good 8.8.8.8\nnochg 1.1.1.1', app.PermanentUpdateError),
                            (b'\xff', app.PermanentUpdateError),
                            (b'x' * 4097, app.PermanentUpdateError)]:
            opener = Mock()
            opener.open.return_value.__enter__ = Mock(return_value=Mock(read=lambda _: body))
            opener.open.return_value.__exit__ = Mock(return_value=False)
            if error:
                with self.assertRaises(error):
                    app.noip_update(config, opener, '8.8.8.8')
            else:
                self.assertEqual(app.noip_update(config, opener, '8.8.8.8'), body.decode())

    def test_failed_discovery_is_rate_limited(self):
        factory = Mock(side_effect=RuntimeError('offline'))
        igd = app.IGDClient(factory=factory)
        with patch.object(app.time, 'monotonic', return_value=100):
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    igd.get_external_ip()
        self.assertEqual(factory.call_count, 1)

    def test_stale_router_is_rediscovered(self):
        router = Mock()
        router.externalipaddress.return_value = '8.8.8.8'
        factory = Mock(return_value=router)
        igd = app.IGDClient(factory=factory)
        igd._u = Mock()
        igd._u.externalipaddress.side_effect = RuntimeError('stale')
        igd._last_discover_ts = 0
        with patch.object(app.time, 'monotonic', return_value=100):
            self.assertEqual(igd.get_external_ip(), '8.8.8.8')
        factory.assert_called_once()

    def test_non_public_ips_never_reach_opener(self):
        for ip in ('0.0.0.0', '10.0.0.1', '172.16.0.1', '192.168.1.1',
                   '100.64.0.1', '127.0.0.1', '169.254.1.1', '224.0.0.1',
                   '::', '::1', 'fc00::1', 'fe80::1', 'ff0e::1', '', None):
            with self.subTest(ip=ip):
                opener = Mock()
                with self.assertRaises(ValueError):
                    app.noip_update({}, opener, ip)
                opener.open.assert_not_called()
                state = {'noip': {}}
                with patch.object(app, 'noip_update') as update:
                    with self.assertRaises(ValueError):
                        app.update_once({}, state, 'unused', Mock(get_external_ip=lambda: ip),
                                        opener, app.UpdateSession(state))
                    update.assert_not_called()

    def test_http_server_errors_require_backoff(self):
        config = {'noip': {'hostname': 'host', 'useragent': 'test'}}
        for code in (500, 503, 599):
            opener = Mock()
            opener.open.side_effect = urllib.error.HTTPError('url', code, 'error', {}, None)
            with self.assertRaises(app.RetryLater):
                app.noip_update(config, opener, '8.8.8.8')

    def test_request_intervals_on_success_and_failure(self):
        for error, interval in ((None, 300), (OSError(), 300), (app.RetryLater(), 1800)):
            with self.subTest(error=error):
                state = {'noip': {}}
                session = app.UpdateSession(state)
                igd = Mock()
                igd.get_external_ip.return_value = '8.8.8.8'
                with patch.object(app.time, 'monotonic', return_value=0) as now, \
                     patch.object(app, 'noip_update', side_effect=error) as update, \
                     patch.object(app, 'atomic_save_json'):
                    if error is None:
                        self.assertTrue(app.update_once({}, state, 'unused', igd, Mock(), session))
                    else:
                        with self.assertRaises(type(error)):
                            app.update_once({}, state, 'unused', igd, Mock(), session)
                        self.assertIsNone(session.extip_prev)
                        self.assertEqual(state, {'noip': {}})
                    igd.get_external_ip.return_value = '1.1.1.1'
                    now.return_value = interval - 0.001
                    with self.assertRaises(app.UpdateDeferred):
                        app.update_once({}, state, 'unused', igd, Mock(), session)
                    self.assertEqual(update.call_count, 1)
                    now.return_value = interval
                    update.side_effect = None
                    self.assertTrue(app.update_once({}, state, 'unused', igd, Mock(), session))
                    self.assertEqual(update.call_count, 2)
                    self.assertEqual(state['noip']['myip'], '1.1.1.1')

    def test_save_retry_does_not_repeat_successful_request(self):
        state = {'noip': {}}
        session = app.UpdateSession(state)
        igd = Mock(get_external_ip=lambda: '8.8.8.8')
        with patch.object(app, 'noip_update') as update, \
             patch.object(app, 'atomic_save_json', side_effect=[OSError(), None]):
            with self.assertRaises(OSError):
                app.update_once({}, state, 'unused', igd, Mock(), session)
            self.assertEqual(session.extip_prev, '8.8.8.8')
            self.assertEqual(state, {'noip': {}})
            self.assertTrue(app.update_once({}, state, 'unused', igd, Mock(), session))
            update.assert_called_once()
            self.assertEqual(state['noip']['myip'], '8.8.8.8')

    def test_empty_discovery_retries_after_cooldown(self):
        router = Mock()
        router.discover.side_effect = [0, 1]
        router.externalipaddress.return_value = '8.8.8.8'
        factory = Mock(return_value=router)
        igd = app.IGDClient(factory=factory)
        with patch.object(app.time, 'monotonic', return_value=0) as now:
            with self.assertRaises(RuntimeError):
                igd.get_external_ip()
            router.selectigd.assert_not_called()
            now.return_value = 59
            with self.assertRaises(RuntimeError):
                igd.get_external_ip()
            self.assertEqual(factory.call_count, 1)
            now.return_value = 60
            self.assertEqual(igd.get_external_ip(), '8.8.8.8')
            self.assertEqual(factory.call_count, 2)

    def test_invalid_rediscovered_router_is_discarded(self):
        router = Mock()
        router.externalipaddress.return_value = '0.0.0.0'
        igd = app.IGDClient(factory=Mock(return_value=router))
        igd._u = Mock()
        igd._u.externalipaddress.side_effect = RuntimeError()
        igd._last_discover_ts = 0
        with patch.object(app.time, 'monotonic', return_value=100):
            with self.assertRaises(ValueError):
                igd.get_external_ip()
        self.assertIsNone(igd._u)

    def test_error_suppression_and_recovery_duration(self):
        tracker = app.ErrorTracker()
        with patch.object(app.time, 'monotonic', return_value=10) as now, \
             self.assertLogs(app.LOG, level='INFO') as logs:
            for error in (OSError('secret'), ValueError('secret'), OSError('changed')):
                tracker.failed(error, 60)
            now.return_value = 135
            tracker.recovered()
            tracker.recovered()
            tracker.failed(OSError('secret'), 60)
        self.assertEqual(len(logs.output), 4)
        self.assertIn('125 seconds', logs.output[2])
        self.assertNotIn('secret', ''.join(logs.output))

    def test_invalid_primary_does_not_replace_valid_backup(self):
        for bad in ('{broken', '{"noip": []}', '{"noip": {"myip": "0.0.0.0"}}'):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'state.json'
                backup = app._bak_name(path)
                backup.write_text('{"noip": {"myip": "8.8.8.8"}}')
                path.write_text(bad)
                self.assertEqual(app.load_state(path)['noip']['myip'], '8.8.8.8')
                app.atomic_save_json(path, backup, {'noip': {'myip': '1.1.1.1'}})
                self.assertEqual(json.loads(backup.read_text())['noip']['myip'], '8.8.8.8')

    def test_replace_failure_preserves_primary_and_cleans_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            backup = app._bak_name(path)
            old = {'noip': {'myip': '8.8.8.8'}}
            app.atomic_save_json(path, backup, old)
            replace = app.os.replace

            def fail_primary(source, target):
                self.assertTrue(path.exists())
                if target == path:
                    raise OSError('replace failed')
                replace(source, target)

            with patch.object(app.os, 'replace', side_effect=fail_primary):
                with self.assertRaises(OSError):
                    app.atomic_save_json(path, backup, {'noip': {'myip': '1.1.1.1'}})
            self.assertEqual(json.loads(path.read_text()), old)
            self.assertEqual(json.loads(backup.read_text()), old)
            self.assertEqual(set(Path(directory).iterdir()), {path, backup})

    def test_serialization_failure_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            with self.assertRaises(TypeError):
                app.atomic_save_json(path, app._bak_name(path), {'noip': {}, 'invalid': object()})
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_main_waits_and_does_not_report_recovery_during_deferral(self):
        stop = Mock()
        stop.is_set.side_effect = [False, False, False, True]
        with patch.object(app.argparse.ArgumentParser, 'parse_args',
                          return_value=Mock(config='config.json', state='state.json', check_config=False)), \
             patch.object(app.threading, 'Event', return_value=stop), \
             patch.object(app.signal, 'signal'), \
             patch.object(app, 'load_config', return_value={}), \
             patch.object(app, 'load_state', return_value={'noip': {}}), \
             patch.object(app, 'noip_build_opener'), \
             patch.object(app, 'IGDClient'), \
             patch.object(app, 'ErrorTracker') as tracker, \
             patch.object(app, 'update_once', side_effect=[app.RetryLater(),
                                                          app.UpdateDeferred(), False]):
            self.assertEqual(app.main(), 0)
        self.assertEqual([call.args[0] for call in stop.wait.call_args_list], [1800, 60, 60])
        tracker.return_value.failed.assert_called_once()
        tracker.return_value.recovered.assert_called_once()


if __name__ == '__main__':
    unittest.main()
