"""Sign-in configuration (no network: MSAL's client is mocked)."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import msal
import msal_extensions

from fortress_onenote_graph import auth


class AuthTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.config = Path(self._dir.name) / "config"

    def test_client_id_comes_from_flag_or_environment_only(self):
        with patch.dict(os.environ, {auth.CLIENT_ID_ENV: ""}):
            with self.assertRaises(auth.AuthError):
                auth.resolve_client_id(None)
            self.assertEqual(auth.resolve_client_id(" abc "), "abc")
        with patch.dict(os.environ, {auth.CLIENT_ID_ENV: "from-env"}):
            self.assertEqual(auth.resolve_client_id(None), "from-env")

    def test_read_only_scopes(self):
        self.assertEqual(auth.SCOPES["personal"], ["Notes.Read", "User.Read"])
        self.assertEqual(auth.SCOPES["work"], ["Notes.Read.All", "User.Read"])
        for scopes in auth.SCOPES.values():
            self.assertFalse(any("Write" in scope or "Create" in scope for scope in scopes))
            # MSAL adds openid/profile/offline_access itself and refuses them in the list.
            self.assertFalse({"openid", "profile", "offline_access"} & {s.lower() for s in scopes})

    def test_keychain_on_macos(self):
        with patch.object(msal_extensions, "KeychainPersistence") as keychain:
            auth.make_persistence(self.config, "client-1", platform="darwin")
        keychain.assert_called_once()
        self.assertEqual(keychain.call_args.kwargs, {"service_name": "fortress-onenote-graph",
                                                    "account_name": "client-1"})

    def test_private_file_elsewhere(self):
        persistence = auth.make_persistence(self.config, "client-1", platform="linux")
        persistence.save("secret")
        mode = stat.S_IMODE(os.stat(persistence.get_location()).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.config).st_mode), 0o700)

    def test_authority_per_account_type(self):
        with patch.object(msal, "PublicClientApplication") as app:
            auth.Session("client-1", self.config)
            self.assertTrue(app.call_args.kwargs["authority"].endswith("/consumers"))
            auth.Session("client-1", self.config, account_type="work")
            self.assertTrue(app.call_args.kwargs["authority"].endswith("/organizations"))
            auth.Session("client-1", self.config, tenant="common")
            self.assertTrue(app.call_args.kwargs["authority"].endswith("/common"))

    def test_browser_then_device_code_fallback(self):
        said = []
        with patch.object(msal, "PublicClientApplication") as factory:
            app = MagicMock()
            factory.return_value = app
            app.get_accounts.return_value = []
            app.acquire_token_interactive.side_effect = RuntimeError("no browser")
            app.initiate_device_flow.return_value = {"user_code": "ABCD", "message": "Go to microsoft.com/devicelogin"}
            app.acquire_token_by_device_flow.return_value = {"access_token": "tok"}
            session = auth.Session("client-1", self.config, say=said.append)
            self.assertEqual(session.token(), "tok")
        self.assertIn("Go to microsoft.com/devicelogin", said)
        app.initiate_device_flow.assert_called_once_with(scopes=["Notes.Read", "User.Read"])

    def test_device_code_is_reported_as_a_json_event(self):
        import io
        import json

        from fortress_onenote_graph.progress import Progress

        stream = io.StringIO()
        progress = Progress("json", stream=stream)
        with patch.object(msal, "PublicClientApplication") as factory:
            app = MagicMock()
            factory.return_value = app
            app.get_accounts.return_value = []
            app.acquire_token_interactive.side_effect = RuntimeError("no browser")
            app.initiate_device_flow.return_value = {
                "user_code": "ABCD-EFGH", "verification_uri": "https://microsoft.com/devicelogin",
                "expires_in": 900, "message": "Go to https://microsoft.com/devicelogin and enter ABCD-EFGH"}
            app.acquire_token_by_device_flow.return_value = {"access_token": "tok"}
            session = auth.Session("client-1", self.config, say=progress.info, on_device_code=progress.device_code)
            self.assertEqual(session.token(), "tok")
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        [code] = [event for event in events if event["event"] == "device_code"]
        self.assertEqual((code["user_code"], code["verification_uri"], code["expires_in"]),
                         ("ABCD-EFGH", "https://microsoft.com/devicelogin", 900))

    def test_cached_account_is_used_silently(self):
        with patch.object(msal, "PublicClientApplication") as factory:
            app = MagicMock()
            factory.return_value = app
            app.get_accounts.return_value = [{"username": "me"}]
            app.acquire_token_silent.return_value = {"access_token": "cached"}
            session = auth.Session("client-1", self.config)
            self.assertEqual(session.token(force_refresh=True), "cached")
        app.acquire_token_interactive.assert_not_called()
        self.assertTrue(app.acquire_token_silent.call_args.kwargs["force_refresh"])


if __name__ == "__main__":
    unittest.main()
