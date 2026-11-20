"""Delegated sign-in with MSAL (public client): browser on a loopback redirect,
device code as the fallback. Read-only scopes only.

* Personal Microsoft accounts: ``Notes.Read`` + ``User.Read`` (personal accounts
  cannot consent to ``Notes.Read.All``).
* Work/school accounts: ``Notes.Read.All`` + ``User.Read`` with ``--account work``
  (also reads notebooks shared with you; your organisation may need to approve it).
  ``Notes.Read`` alone also works for your own notebooks.

MSAL always adds ``openid``, ``profile`` and ``offline_access`` (the refresh token)
itself; it refuses them in the scope list.

The client id is the user's own Entra app registration (README): the tool ships
none. Tokens are cached in the macOS Keychain on macOS, elsewhere in a file
readable only by the user (0600).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable

CLIENT_ID_ENV = "FORTRESS_ONENOTE_CLIENT_ID"
TENANT_ENV = "FORTRESS_ONENOTE_TENANT"
CONSUMER_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
SCOPES = {
    "personal": ["Notes.Read", "User.Read"],
    "work": ["Notes.Read.All", "User.Read"],
}
KEYCHAIN_SERVICE = "fortress-onenote-graph"


class AuthError(RuntimeError):
    pass


def resolve_client_id(flag: str | None) -> str:
    client_id = (flag or os.environ.get(CLIENT_ID_ENV) or "").strip()
    if not client_id:
        raise AuthError(
            f"No client id. Register a free app in Microsoft Entra (see the README) and pass "
            f"--client-id or set {CLIENT_ID_ENV}.")
    return client_id


def token_cache_path(config_dir: Path) -> Path:
    return config_dir / "msal_token_cache.bin"


def make_persistence(config_dir: Path, client_id: str, platform: str = sys.platform):
    """Keychain on macOS, a 0600 file elsewhere (msal-extensions)."""

    import msal_extensions

    config_dir.mkdir(parents=True, exist_ok=True)
    try:
        config_dir.chmod(0o700)
    except OSError:
        pass
    location = str(token_cache_path(config_dir))
    if platform == "darwin":
        return msal_extensions.KeychainPersistence(location, service_name=KEYCHAIN_SERVICE,
                                                   account_name=client_id)
    return msal_extensions.FilePersistence(location)


class Session:
    """An MSAL public client with a persistent cache; ``token(force)`` for GraphClient."""

    def __init__(self, client_id: str, config_dir: Path, *, account_type: str = "personal",
                 tenant: str | None = None, device_code: bool = False,
                 say: Callable[[str], None] = lambda message: print(message, file=sys.stderr)):
        import msal
        import msal_extensions

        if account_type not in SCOPES:
            raise AuthError(f"--account must be one of {', '.join(SCOPES)}")
        self.scopes = SCOPES[account_type]
        self.device_code = device_code
        self.say = say
        tenant = (tenant or os.environ.get(TENANT_ENV) or
                  ("consumers" if account_type == "personal" else "organizations")).strip()
        cache = msal_extensions.PersistedTokenCache(make_persistence(config_dir, client_id))
        self.app = msal.PublicClientApplication(
            client_id, authority=f"https://login.microsoftonline.com/{tenant}", token_cache=cache)

    def token(self, force_refresh: bool = False) -> str:
        for account in self.app.get_accounts():
            result = self.app.acquire_token_silent(self.scopes, account=account, force_refresh=force_refresh)
            if result and "access_token" in result:
                return result["access_token"]
        return self.login()["access_token"]

    def login(self) -> dict:
        result: dict | None = None
        if not self.device_code:
            try:
                self.say("Opening your browser to sign in to Microsoft (read-only access to OneNote)…")
                result = self.app.acquire_token_interactive(self.scopes, prompt="select_account")
            except Exception as exc:  # no browser, no loopback port, ...
                self.say(f"Browser sign-in did not work ({exc}); using a device code instead.")
                result = None
        if not result or "access_token" not in result:
            flow = self.app.initiate_device_flow(scopes=self.scopes)
            if "user_code" not in flow:
                raise AuthError(f"Could not start device-code sign-in: {flow.get('error_description') or flow}")
            self.say(flow["message"])
            result = self.app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise AuthError(f"Sign-in failed: {result.get('error_description') or result.get('error')}")
        return result

    def logout(self) -> int:
        accounts = self.app.get_accounts()
        for account in accounts:
            self.app.remove_account(account)
        return len(accounts)
