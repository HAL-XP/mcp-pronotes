"""Credential-storage tests using fictional tokens and mocked network clients."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import auth


class AuthTests(unittest.TestCase):
    def setUp(self):
        preferred = Path("/tmp/opencode")
        self.temp = tempfile.TemporaryDirectory(dir=preferred if preferred.is_dir() else None)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "login.json"
        self.credentials = {
            "pronote_url": "https://example.invalid/pronote/mobile.eleve.html",
            "username": "fictional-student", "password": "fictional-token",
            "uuid": "fictional-device", "client_identifier": None,
        }
        self.client = Mock(logged_in=True)
        self.client.export_credentials.return_value = self.credentials
        self.student_patch = patch.object(auth.pronotepy, "Client")
        self.student = self.student_patch.start()
        self.student.qrcode_login.return_value = self.client
        self.student.token_login.return_value = self.client
        self.addCleanup(self.student_patch.stop)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def store(self, value):
        auth.write_private_json(self.path, value)

    def test_atomic_private_write(self):
        self.store({"password": "fictional"})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.path.read_text()), {"password": "fictional"})
        self.assertEqual(list(self.path.parent.glob(".pronote-login-*")), [])

    def test_qr_bootstrap_replaces_payload_with_token(self):
        qr = {"url": self.credentials["pronote_url"], "login": "fake-encrypted-login", "jeton": "fake-encrypted-token"}
        self.store({"qr_code": qr, "pin": "1234", "account_type": "student"})
        client, account, _ = auth.login_from_file(self.path, "parent")
        self.assertIs(client, self.client)
        self.assertEqual(account, "student")
        args, kwargs = self.student.qrcode_login.call_args
        self.assertEqual(args[:2], (qr, "1234"))
        self.assertTrue(args[2])
        self.assertTrue(kwargs["skip_2fa"])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["auth_mode"], "token")
        self.assertEqual(saved["credentials"], self.credentials)
        self.assertNotIn("pin", saved)
        self.assertNotIn("qr_code", saved)

    def test_raw_qr_uses_environment_pin(self):
        self.store({"url": self.credentials["pronote_url"], "login": "fake", "jeton": "fake"})
        with patch.dict(os.environ, {"PRONOTE_QR_PIN": "4321"}):
            auth.login_from_file(self.path, "student")
        self.assertEqual(self.student.qrcode_login.call_args.args[1], "4321")

    def test_token_login_and_rotation(self):
        self.store({"account_type": "student", "credentials": self.credentials})
        self.client.export_credentials.return_value = {**self.credentials, "password": "rotated-fictional-token"}
        auth.login_from_file(self.path, "parent")
        self.student.token_login.assert_called_once_with(**self.credentials)
        self.assertEqual(json.loads(self.path.read_text())["credentials"]["password"], "rotated-fictional-token")

    def test_flat_exported_credentials(self):
        self.store(self.credentials)
        auth.login_from_file(self.path, "student")
        self.student.token_login.assert_called_once_with(**self.credentials)

    def test_invalid_pin_never_connects(self):
        for pin in (None, "123", "abcd", 1234, "１２３４"):
            with self.subTest(pin=pin):
                self.store({"qr_code": {"url": self.credentials["pronote_url"], "login": "fake", "jeton": "fake"}, "pin": pin})
                with self.assertRaises(ValueError):
                    auth.login_from_file(self.path, "student")
        self.student.qrcode_login.assert_not_called()

    def test_invalid_payload_never_connects(self):
        for value in ([], {}, {"credentials": {}}, {"qr_code": "not-a-dict", "pin": "1234"}):
            with self.subTest(value=value):
                self.store(value)
                with self.assertRaises(ValueError):
                    auth.login_from_file(self.path, "student")
        self.student.qrcode_login.assert_not_called()
        self.student.token_login.assert_not_called()

    def test_url_must_match_student_account(self):
        self.store({**self.credentials, "pronote_url": "https://example.invalid/pronote/parent.html"})
        with self.assertRaises(ValueError):
            auth.login_from_file(self.path, "student")
        self.student.token_login.assert_not_called()

    def test_rejected_login_does_not_save_bad_credentials(self):
        self.store(self.credentials)
        self.client.logged_in = False
        with self.assertRaises(ValueError):
            auth.login_from_file(self.path, "student")
        self.assertEqual(json.loads(self.path.read_text()), self.credentials)

    def test_symlink_is_rejected(self):
        target = self.path.parent / "original.json"
        target.write_text("{}")
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            auth.write_private_json(self.path, {"changed": True})
        with self.assertRaises(ValueError):
            auth.login_from_file(self.path, "student")
        self.assertEqual(target.read_text(), "{}")

    def test_failed_qr_preserves_device_id_for_retry(self):
        self.store({"qr_code": {"url": self.credentials["pronote_url"], "login": "fake", "jeton": "fake"}, "pin": "1234"})
        self.student.qrcode_login.side_effect = RuntimeError("Fictional connection failure")
        with self.assertRaises(RuntimeError):
            auth.login_from_file(self.path, "student")
        device_id = json.loads(self.path.read_text())["uuid"]
        with self.assertRaises(RuntimeError):
            auth.login_from_file(self.path, "student")
        self.assertEqual(self.student.qrcode_login.call_args.args[2], device_id)

    def test_failed_write_preserves_previous_credentials(self):
        self.store({"password": "old-fictional-token"})
        with patch.object(auth.os, "replace", side_effect=OSError("Fictional disk error")):
            with self.assertRaises(OSError):
                self.store({"password": "new-fictional-token"})
        self.assertEqual(json.loads(self.path.read_text())["password"], "old-fictional-token")
        self.assertEqual(list(self.path.parent.glob(".pronote-login-*")), [])
