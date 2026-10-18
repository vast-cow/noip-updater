import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import manage_install as installer
import noip_updater as app


class InstallationTests(unittest.TestCase):
    def test_update_preserves_data_and_failed_preparation_preserves_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = (root / 'data', root / 'config', root / 'state', root / 'user/app.service')
            with patch('venv.EnvBuilder.create'), patch.object(installer, 'run') as run:
                installer.install(*paths)
                first = (paths[0] / 'current').resolve()
                config = paths[1] / 'config.json'
                config.write_text('custom credentials')
                state = paths[2] / 'state.json'
                state.write_text('saved state')
                installer.install(*paths)
                self.assertNotEqual(first, (paths[0] / 'current').resolve())
                self.assertTrue(first.exists())
                self.assertEqual(config.read_text(), 'custom credentials')
                self.assertEqual(state.read_text(), 'saved state')
                unit = paths[3].read_text()
                self.assertIn(str(config), unit)
                self.assertNotIn('@EXEC_START@', unit)
                self.assertFalse(any('restart' in call.args or 'stop' in call.args for call in run.call_args_list))
                previous = (paths[0] / 'current').resolve()
                run.side_effect = subprocess.CalledProcessError(1, 'pip')
                with self.assertRaises(subprocess.CalledProcessError):
                    installer.install(*paths)
                self.assertEqual((paths[0] / 'current').resolve(), previous)
                self.assertEqual(paths[3].read_text(), unit)
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_check_is_offline_and_does_not_modify_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.json'
            state = root / 'state.json'
            config.write_text(json.dumps({'noip': dict(username='key', password='secret',
                              hostname='all.ddnskey.com', useragent='client/1 me@example.com')}))
            with patch.object(app, 'IGDClient', side_effect=AssertionError), \
                 patch.object(app, 'noip_build_opener', side_effect=AssertionError), \
                 patch('sys.argv', ['updater', '--config', str(config), '--state', str(state), '--check-config']):
                self.assertEqual(app.main(), 0)
            self.assertFalse(state.exists())
            config.write_text(Path('config.example.json').read_text())
            with self.assertRaises(ValueError):
                app.check_config(config, state)

    def test_uninstall_preserves_data_unless_purged(self):
        for purge in (False, True):
            with self.subTest(purge=purge), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths = (root / 'data', root / 'config', root / 'state', root / 'user/app.service')
                for path in paths[:3]:
                    path.mkdir()
                    (path / 'keep').write_text('existing')
                paths[3].parent.mkdir()
                paths[3].write_text('unit')
                dropin = paths[3].parent / 'noip-updater.service.d'
                dropin.mkdir()
                argv = ['manage', 'uninstall'] + (['--purge'] if purge else [])
                with patch.object(installer, 'locations', return_value=paths), \
                     patch.object(installer, 'run') as run, \
                     patch.object(installer.subprocess, 'run'), \
                     patch('sys.argv', argv):
                    old_mask = os.umask(0o077)
                    try:
                        self.assertEqual(installer.main(), 0)
                    finally:
                        os.umask(old_mask)
                self.assertFalse(paths[0].exists())
                self.assertFalse(paths[3].exists())
                self.assertTrue(dropin.exists())
                self.assertEqual(paths[1].exists(), not purge)
                self.assertEqual(paths[2].exists(), not purge)
                run.assert_any_call('systemctl', '--user', 'disable', '--now', 'noip-updater.service')

    def test_xdg_paths(self):
        with patch.dict(os.environ, {'XDG_DATA_HOME': '/tmp/custom data',
                                     'XDG_CONFIG_HOME': '/tmp/config', 'XDG_STATE_HOME': '/tmp/state'}):
            self.assertEqual(installer.locations()[0], Path('/tmp/custom data/noip-updater'))
        self.assertIn('%%', installer.unit_quote('/tmp/a%b'))
        self.assertIn('$$', installer.unit_quote('/tmp/a$b'))
