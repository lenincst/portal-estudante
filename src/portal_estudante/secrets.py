"""Where each student's own credentials and session live. Never in code, never in .env, never sent anywhere but the portal.

Default (public): the password is typed by the student on the official sign-in page; only the resulting session is kept,
in an encrypted file (Fernet/AES) under the private data dir, whose key lives in the operating-system keychain
(`keyring`: Windows Credential Manager / macOS Keychain / Secret Service). Saving the password itself is OPT-IN
(`setup --save-password`) and goes only to the OS keychain. Values are never logged; `repr()` shows only keys.
Other stores can be plugged in as an extension module (config `backend: extension`).
Keys: ULIFE_USERNAME, ULIFE_PASSWORD (optional), ULIFE_SESSION (created by the app).
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import keyring

from . import config
from .errors import SecretNotFoundError, SecretsError

KEYRING_SERVICE = config.KEYRING_SERVICE
CONFIG_ENTRY = "CONFIG"  # small JSON: {"backend": "local"} or {"backend": "extension", "module": ..., "class": ..., "args": {...}}
FERNET_ENTRY = "LOCAL_VAULT_KEY"
SMALL_LIMIT = 1000  # values above this go to the encrypted file in the local backend


class Store(Protocol):
    def get(self, key: str) -> str: ...
    def get_optional(self, key: str) -> str | None: ...
    def put(self, key: str, value: str, note: str = "") -> None: ...
    def keys(self) -> list[str]: ...


def load_config() -> dict[str, Any]:
    raw = keyring.get_password(KEYRING_SERVICE, CONFIG_ENTRY)
    if raw:
        return json.loads(raw)
    return {}


def save_config(cfg: dict[str, Any]) -> None:
    keyring.set_password(KEYRING_SERVICE, CONFIG_ENTRY, json.dumps(cfg))


class LocalStore:
    """OS keychain (+ Fernet-encrypted file for large values)."""

    def __init__(self) -> None:
        self._dir = config.DATA_DIR / "vault"

    def __repr__(self) -> str:
        return f"LocalStore(keys={self.keys()})"

    def _fernet(self) -> Any:
        from cryptography.fernet import Fernet

        key = keyring.get_password(KEYRING_SERVICE, FERNET_ENTRY)
        if not key:
            key = Fernet.generate_key().decode()
            keyring.set_password(KEYRING_SERVICE, FERNET_ENTRY, key)
        return Fernet(key.encode())

    def get_optional(self, key: str) -> str | None:
        value = keyring.get_password(KEYRING_SERVICE, key)
        if value is not None:
            return value
        path = self._dir / f"{key}.enc"
        if path.exists():
            return self._fernet().decrypt(path.read_bytes()).decode()
        return None

    def get(self, key: str) -> str:
        value = self.get_optional(key)
        if value is None:
            raise SecretNotFoundError(key)
        return value

    def put(self, key: str, value: str, note: str = "") -> None:
        if len(value) <= SMALL_LIMIT:
            keyring.set_password(KEYRING_SERVICE, key, value)
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{key}.enc").write_bytes(self._fernet().encrypt(value.encode()))

    def keys(self) -> list[str]:
        found = [k for k in ("ULIFE_USERNAME", "ULIFE_PASSWORD") if keyring.get_password(KEYRING_SERVICE, k)]
        if self._dir.exists():
            found += [p.stem for p in self._dir.glob("*.enc")]
        return sorted(found)


def SecretStore() -> Store:  # noqa: N802 - kept as the historical constructor name
    """Return the configured backend (see module docstring)."""
    cfg = load_config()
    backend = cfg.get("backend")
    if backend == "extension":  # optional store provided by a separate module (not part of the public release)
        import importlib

        try:
            cls = getattr(importlib.import_module(cfg["module"]), cfg["class"])
        except (ImportError, AttributeError, KeyError) as exc:
            raise SecretsError("Configured credential-store extension is not available in this installation.") from exc
        return cls(**cfg.get("args", {}))
    if backend == "local":
        return LocalStore()
    raise SecretsError("Not configured yet. Run `python -m portal_estudante setup` once (it asks only for your login).")
