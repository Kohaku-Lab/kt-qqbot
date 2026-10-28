"""Locate kt-qqbot files under the KohakuTerrarium config directory."""

from pathlib import Path

from kohakuterrarium.utils.config_dir import config_subdir

ROOT_NAME = "qq-bot"


def root_dir() -> Path:
    """Return ``<config>/qq-bot``, which holds credentials, data and character files."""
    return config_subdir(ROOT_NAME)


def credentials_path() -> Path:
    """Return the credential store file."""
    return root_dir() / "credentials.json"


def vault_path(app_id: str) -> Path:
    """Return the on-disk chat store of one bot."""
    return config_subdir(ROOT_NAME, "data") / f"{app_id}.kvault"


def media_dir() -> Path:
    """Return the folder for downloaded inbound media."""
    return config_subdir(ROOT_NAME, "media")


def lock_path(app_id: str) -> Path:
    """Return the per-bot lock file that keeps one process per bot."""
    return config_subdir(ROOT_NAME, "data") / f"{app_id}.lock"
