import json
from pathlib import Path
import tempfile
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
                                        'unused', Mock(get_external_ip=lambda: '8.8.8.8'), opener))
        opener.open.assert_not_called()

    def test_failure_does_not_advance_state(self):
        state = {'noip': {}}
        with patch.object(app, 'noip_update', side_effect=app.RetryLater), \
             patch.object(app, 'atomic_save_json') as save:
            with self.assertRaises(app.RetryLater):
                app.update_once({}, state, 'unused', Mock(get_external_ip=lambda: '8.8.8.8'), Mock())
            self.assertEqual(state, {'noip': {}})
            save.assert_not_called()

    def test_protocol_responses(self):
        config = {'noip': {'hostname': 'example.ddns.net', 'useragent': 'test/1 user@example.com'}}
        for body, error in [(b'good 8.8.8.8', None), (b'nochg 8.8.8.8', None),
                            (b'badauth', app.PermanentUpdateError), (b'911', app.RetryLater)]:
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


if __name__ == '__main__':
    unittest.main()
