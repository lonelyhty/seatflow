"""Dummy-only comparisons against the unchanged, public browser function."""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from seat_reserver.protocol_signing import PublicSubmitSigner, SigningError
from seat_reserver.signing_assets import asset_directory, verify_assets, SigningAssetsError


_REFERENCE = r"""
const fs = require('fs'), vm = require('vm');
const requests = JSON.parse(fs.readFileSync(0, 'utf8'));
const context = vm.createContext({});
vm.runInContext(`
  var window = this, pageValue = '', exported;
  var document = {getElementById: function(name) {
    if(name !== 'submit_enc') throw Error('wrong element');
    return {value: pageValue};
  }};
  function define(dependencies, factory) { exported = factory(); }
`, context, {timeout: 2000});
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context, {timeout: 2000});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context, {timeout: 2000});
const results = requests.map(request => {
  context.inputJson = JSON.stringify(request);
  return vm.runInContext(`
    var input = JSON.parse(inputJson); pageValue = input.submit_enc;
    exported.verifyParam(input.parameters);
  `, context, {timeout: 2000});
});
process.stdout.write(JSON.stringify(results));
"""


def dummy_parameters():
    return {"deptIdEnc": "dummy-department", "roomId": "1234", "day": "2026-10-12",
            "startTime": "20:00", "endTime": "21:00", "seatNum": "0151",
            "captcha": "", "wyToken": ""}


@unittest.skipUnless(shutil.which("node"), "Node is required to run the normal public module")
class PublicSubmitSignerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            verify_assets(asset_directory())
        except SigningAssetsError:
            raise unittest.SkipTest('Optional upstream JavaScript is not distributed; see docs/PROTOCOL.md')
        cls.signer = PublicSubmitSigner().start()

    @classmethod
    def tearDownClass(cls):
        cls.signer.close()

    def reference(self, cases):
        response = subprocess.run(
            [shutil.which("node"), "-e", _REFERENCE,
             str(self.signer.signing_js), str(self.signer.md5_js)],
            input=json.dumps(cases, ensure_ascii=True), text=True,
            encoding="utf-8", capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True,
        )
        return json.loads(response.stdout)

    def test_dummy_parameters_match_normal_public_function(self):
        parameters = dummy_parameters()
        expected = self.reference([{"parameters": parameters, "submit_enc": "dummy-page-key"}])[0]
        self.assertEqual(self.signer.sign(parameters, "dummy-page-key"), expected)

    def test_unicode_and_javascript_metacharacters_keep_original_semantics(self):
        parameters = dummy_parameters()
        parameters["captcha"] = "测试🙂'\\\n\u2028\u2029"
        parameters["wyToken"] = "dummy[$&=]token"
        expected = self.reference([{"parameters": parameters, "submit_enc": "假页面值🙂"}])[0]
        self.assertEqual(self.signer.sign(parameters, "假页面值🙂"), expected)

    def test_field_order_and_leading_zero_labels_are_preserved(self):
        first = dummy_parameters()
        second = dict(reversed(list(first.items())))
        expected = self.reference([
            {"parameters": first, "submit_enc": "dummy-page-key"},
            {"parameters": second, "submit_enc": "dummy-page-key"},
        ])
        self.assertEqual(self.signer.sign(first, "dummy-page-key"), expected[0])
        self.assertEqual(self.signer.sign(second, "dummy-page-key"), expected[1])
        changed = dict(first, seatNum="151")
        self.assertNotEqual(self.signer.sign(changed, "dummy-page-key"), expected[0])

    def test_challenge_fields_and_page_value_affect_normal_result(self):
        base = dummy_parameters()
        cases = [
            {"parameters": base, "submit_enc": "dummy-page-key"},
            {"parameters": dict(base, captcha="dummy-captcha"), "submit_enc": "dummy-page-key"},
            {"parameters": dict(base, wyToken="dummy-token"), "submit_enc": "dummy-page-key"},
            {"parameters": base, "submit_enc": "different-dummy-key"},
        ]
        expected = self.reference(cases)
        actual = [self.signer.sign(case["parameters"], case["submit_enc"]) for case in cases]
        self.assertEqual(actual, expected)
        self.assertEqual(len(set(actual)), len(cases))

    def test_input_is_not_mutated(self):
        parameters = dummy_parameters()
        original = copy.deepcopy(parameters)
        self.signer.sign(parameters, "dummy-page-key")
        self.assertEqual(parameters, original)

    def test_hot_path_reuses_worker_without_starting_a_process(self):
        pid = self.signer.process.pid
        with patch("seat_reserver.protocol_signing.subprocess.Popen", side_effect=AssertionError("cold start")):
            first = self.signer.sign(dummy_parameters(), "dummy-page-key")
            second = self.signer.sign(dummy_parameters(), "dummy-page-key")
        self.assertEqual(first, second)
        self.assertEqual(self.signer.process.pid, pid)

    def test_invalid_input_fails_before_ipc_and_does_not_expose_values(self):
        invalid = [({}, "dummy"), ({"roomId": 1234}, "dummy"),
                   ({"__proto__": "secret-dummy-value"}, "dummy"),
                   (dummy_parameters(), ""), (dummy_parameters(), None)]
        with patch.object(self.signer, "_response", side_effect=AssertionError("unexpected IPC")):
            for parameters, key in invalid:
                with self.subTest(parameters=parameters, key=key):
                    with self.assertRaises(SigningError) as caught:
                        self.signer.sign(parameters, key)
                    self.assertNotIn("secret-dummy-value", str(caught.exception))

    def test_metadata_contains_only_public_asset_hashes(self):
        self.assertEqual(set(self.signer.metadata), {"signing_sha256", "md5_sha256"})
        for value in self.signer.metadata.values():
            self.assertRegex(value, r"^[0-9a-f]{64}$")

    def test_not_started_or_closed_worker_cannot_sign(self):
        other = PublicSubmitSigner()
        with self.assertRaises(SigningError):
            other.sign(dummy_parameters(), "dummy-page-key")
        other.start()
        process = other.process
        other.close()
        self.assertIsNotNone(process.poll())
        with self.assertRaises(SigningError):
            other.sign(dummy_parameters(), "dummy-page-key")

    def test_missing_public_asset_has_generic_error(self):
        other = PublicSubmitSigner(Path(__file__).parent / "missing-dummy.js", self.signer.md5_js)
        with self.assertRaises(SigningError):
            other.start()


if __name__ == "__main__":
    unittest.main()
