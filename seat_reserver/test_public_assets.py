"""Offline packaging and dependency checks using original synthetic fixtures."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .credentials import data_dir
from .protocol_signing import PublicSubmitSigner, SigningError
from .signing_assets import ASSET_HASHES, SigningAssetsError, import_assets, verify_assets


class PublicAssetsTests(unittest.TestCase):
    def test_unconfigured_public_data_does_not_use_legacy_accounts(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'LOCALAPPDATA': folder}):
            original = os.environ.pop('SEATFLOW_DATA_DIR', None)
            try:
                self.assertEqual(data_dir(), Path(folder) / 'SeatFlow')
            finally:
                if original is not None:
                    os.environ['SEATFLOW_DATA_DIR'] = original

    def test_wrong_assets_cannot_replace_existing_dependency(self):
        with tempfile.TemporaryDirectory() as folder:
            source, destination = Path(folder) / 'source', Path(folder) / 'destination'
            source.mkdir()
            destination.mkdir()
            for name in ASSET_HASHES:
                (source / name).write_bytes(b'changed-fixture-only')
                (destination / name).write_bytes(b'original-fixture-only')
            with self.assertRaises(SigningAssetsError):
                import_assets(source, destination)
            for name in ASSET_HASHES:
                self.assertEqual((destination / name).read_bytes(), b'original-fixture-only')

    def test_all_files_are_validated_before_import_and_remain_identical(self):
        contents = {'submitVerify.min.js': b'synthetic-signing', 'md5.js': b'synthetic-digest'}
        hashes = {name: hashlib.sha256(value).hexdigest() for name, value in contents.items()}
        with tempfile.TemporaryDirectory() as folder, patch.dict(ASSET_HASHES, hashes, clear=True):
            source, destination = Path(folder) / 'source', Path(folder) / 'destination'
            source.mkdir()
            (source / 'submitVerify.min.js').write_bytes(contents['submitVerify.min.js'])
            with self.assertRaises(SigningAssetsError):
                import_assets(source, destination)
            self.assertFalse(destination.exists())
            (source / 'md5.js').write_bytes(contents['md5.js'])
            import_assets(source, destination)
            self.assertEqual(verify_assets(destination), hashes)
            for name, content in contents.items():
                self.assertEqual((destination / name).read_bytes(), content)

    def test_missing_default_assets_stop_before_node_is_started(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'SEATFLOW_SIGNING_DIR': folder}), patch(
                'seat_reserver.protocol_signing.subprocess.Popen') as process:
            with self.assertRaisesRegex(SigningError, '本地依赖'):
                PublicSubmitSigner().start()
            process.assert_not_called()

    @unittest.skipUnless(shutil.which('node'), 'Node is needed for the synthetic subprocess check')
    def test_subprocess_reuses_worker_and_loads_only_explicit_synthetic_modules(self):
        # Deliberately invalid as a real reservation signature: fixtures only test
        # module loading, value transport, worker reuse and process cleanup.
        script = '''define(['md5'], function() { return { verifyParam: function(p) {
            if (document.getElementById('submit_enc').value !== 'fixture-key') throw Error('key');
            return hex_md5(p.value);
        }}; });'''
        digest = 'function hex_md5(value) { return value === "fixture-one" ? "1".repeat(32) : "2".repeat(32); }'
        with tempfile.TemporaryDirectory() as folder:
            signing, md5 = Path(folder) / 'signing.js', Path(folder) / 'digest.js'
            signing.write_text(script, encoding='utf-8')
            md5.write_text(digest, encoding='utf-8')
            with PublicSubmitSigner(signing, md5) as signer:
                process = signer.process
                self.assertEqual(signer.sign({'value': 'fixture-one'}, 'fixture-key'), '1' * 32)
                with patch('seat_reserver.protocol_signing.subprocess.Popen', side_effect=AssertionError('unexpected restart')):
                    self.assertEqual(signer.sign({'value': 'fixture-two'}, 'fixture-key'), '2' * 32)
                self.assertIs(signer.process, process)
            self.assertIsNotNone(process.poll())


if __name__ == '__main__':
    unittest.main()
