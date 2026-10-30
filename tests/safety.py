"""Fail-closed test guard: no real LLM provider, no real credentials, no real network.

* Provider factories and real-provider construction are handled by the core's
  ``kohakuterrarium.testing.llm_seam``; every provider subclass is blocked, not
  only the base class.
* Credential paths that the core freezes at import time are pointed into tmp,
  and an audit hook refuses to open anything under the real home's
  credential folders.
* Socket connects and name lookups are allowed only for loopback.
"""

import ipaddress
import os
import socket
import sys
from pathlib import Path

import kohakuterrarium.llm.codex.auth as codex_auth_mod
import kohakuterrarium.llm.profiles.api_keys as api_keys_mod
import kohakuterrarium.llm.profiles.backends as backends_mod
import kohakuterrarium.studio.identity.mcp.servers as mcp_servers_mod
import kohakuterrarium.studio.identity.ui_prefs as ui_prefs_mod
from kohakuterrarium.llm.base import BaseLLMProvider
from kohakuterrarium.testing.llm import ScriptedLLM
from kohakuterrarium.testing.llm_seam import (
    RealProviderBlocked,
    forbid_real_providers,
    install_llm_seam,
)

REAL_HOME = Path(os.path.expanduser("~")).resolve()
SECRET_DIRS = tuple(
    str(REAL_HOME / name)
    for name in (
        ".codex",
        ".kohakuterrarium",
        ".gemini",
        ".claude",
        ".config",
        ".aws",
        ".ssh",
        ".netrc",
        ".docker",
    )
)
# The core writes its run logs here; they hold no credentials.
ALLOWED_DIRS = (str(REAL_HOME / ".kohakuterrarium" / "logs"),)
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "ip6-localhost"}

_active = {"on": False, "network": True}


class GuardViolation(PermissionError):
    """A test tried to read real credentials or reach the network."""


def _loopback(host: object) -> bool:
    if host is None:
        return True
    text = str(host)
    if text in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(text.split("%")[0]).is_loopback
    except ValueError:
        return False


def _audit(event: str, args: tuple) -> None:
    if not _active["on"]:
        return
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        path = os.fsdecode(args[0])
        try:
            real = os.path.realpath(path)
        except (OSError, ValueError):
            return
        if real.startswith(SECRET_DIRS) and not real.startswith(ALLOWED_DIRS):
            raise GuardViolation(f"test opened a real credential path: {real}")
    elif not _active["network"]:
        return
    elif event == "socket.connect":
        address = args[1] if len(args) > 1 else None
        host = address[0] if isinstance(address, tuple) and address else address
        if isinstance(host, str) and not host.startswith("/") and not _loopback(host):
            raise GuardViolation(f"test tried to connect to {address!r}")
    elif event == "socket.getaddrinfo":
        host = args[0] if args else None
        if isinstance(host, bytes):
            host = host.decode()
        if not _loopback(host):
            raise socket.gaierror(
                socket.EAI_NONAME, f"blocked lookup of {host!r} in tests"
            )


sys.addaudithook(_audit)


def _provider_classes() -> list[type]:
    """Return BaseLLMProvider and every subclass loaded so far."""
    found, todo = [], [BaseLLMProvider]
    while todo:
        cls = todo.pop()
        found.append(cls)
        todo.extend(cls.__subclasses__())
    return found


def _scripted() -> ScriptedLLM:
    return ScriptedLLM(["(scripted)"])


def install(monkeypatch, tmp_path: Path, *, network: bool = True) -> None:
    """Patch every factory, credential path and the network for one test."""
    install_llm_seam(monkeypatch, _scripted)
    classes = _provider_classes()
    forbid_real_providers(
        lambda _base, name, blocked: [
            monkeypatch.setattr(cls, name, blocked) for cls in classes
        ]
    )
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    kt_dir = home / ".kohakuterrarium"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setattr(
        codex_auth_mod, "CODEX_CLI_TOKEN_PATH", home / ".codex" / "auth.json"
    )
    monkeypatch.setattr(
        codex_auth_mod, "DEFAULT_TOKEN_PATH", kt_dir / "codex-auth.json"
    )
    monkeypatch.setattr(api_keys_mod, "KT_DIR", kt_dir)
    monkeypatch.setattr(backends_mod, "PROFILES_PATH", kt_dir / "llm_profiles.yaml")
    monkeypatch.setattr(ui_prefs_mod, "KT_DIR", kt_dir)
    monkeypatch.setattr(mcp_servers_mod, "KT_DIR", kt_dir)
    for var in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "BRAVE_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    _active["on"] = True
    _active["network"] = network


def uninstall() -> None:
    """Turn the audit hook's checks off after a test."""
    _active["on"] = False
    _active["network"] = True


__all__ = [
    "GuardViolation",
    "REAL_HOME",
    "RealProviderBlocked",
    "install",
    "uninstall",
]
