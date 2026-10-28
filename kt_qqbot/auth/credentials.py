"""Store bot AppID / AppSecret pairs in a private JSON file.

The file is ``<config>/qq-bot/credentials.json``, written atomically with
mode 0600. ``QQBOT_APPID`` / ``QQBOT_SECRET`` override it.
"""

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from kt_qqbot.errors import CredentialsMissing
from kt_qqbot.filelock import FileLock
from kt_qqbot.paths import credentials_path

ENV_APP_ID = "QQBOT_APPID"
ENV_SECRET = "QQBOT_SECRET"


@dataclass(frozen=True)
class Credentials:
    """One bot's platform credentials."""

    app_id: str
    app_secret: str

    def __repr__(self) -> str:
        return f"Credentials(app_id={self.app_id!r}, app_secret=***)"


class CredentialStore:
    """Read and write the credential file."""

    def __init__(self, path: Path | None = None):
        self.path = path or credentials_path()

    def _read(self) -> dict:
        if not self.path.exists():
            return {"default": None, "bots": {}}
        data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        data.setdefault("default", None)
        data.setdefault("bots", {})
        return data

    def _lock(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(self.path.with_name(self.path.name + ".lock"))

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def save(self, creds: Credentials, *, make_default: bool = True) -> None:
        """Add or replace one bot's credentials (locked read-modify-write)."""
        with self._lock():
            data = self._read()
            data["bots"][creds.app_id] = {
                "app_secret": creds.app_secret,
                "bound_at": int(time.time()),
            }
            if make_default or not data["default"]:
                data["default"] = creds.app_id
            self._write(data)

    def remove(self, app_id: str | None = None) -> str | None:
        """Delete one bot (the default when ``app_id`` is None); return the removed id."""
        with self._lock():
            data = self._read()
            target = app_id or data["default"]
            if not target or target not in data["bots"]:
                return None
            del data["bots"][target]
            if data["default"] == target:
                data["default"] = next(iter(data["bots"]), None)
            self._write(data)
            return target

    def list_app_ids(self) -> list[str]:
        """Return every stored AppID."""
        return list(self._read()["bots"])

    def default_app_id(self) -> str | None:
        """Return the AppID used when none is named."""
        return self._read()["default"]

    def load(self, app_id: str | None = None) -> Credentials:
        """Resolve credentials: environment first, then the file.

        Raises :class:`CredentialsMissing` when nothing matches.
        """
        env_id = os.environ.get(ENV_APP_ID, "").strip()
        env_secret = os.environ.get(ENV_SECRET, "").strip()
        if env_id and env_secret and (app_id is None or app_id == env_id):
            return Credentials(env_id, env_secret)
        data = self._read()
        target = app_id or data["default"]
        entry = data["bots"].get(target) if target else None
        if not entry or not entry.get("app_secret"):
            raise CredentialsMissing(app_id)
        return Credentials(str(target), entry["app_secret"])
