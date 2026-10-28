"""Fail-closed test guard: no real LLM provider, no real credentials, no real network.

* Every LLM factory the core calls is replaced by a ScriptedLLM factory, and
  ``BaseLLMProvider.__init__`` raises, so a missed factory still cannot build a
  real provider.
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

import kohakuterrarium.bootstrap.agent_init as agent_init_mod
import kohakuterrarium.bootstrap.llm as bootstrap_llm_mod
import kohakuterrarium.core.agent_compact as agent_compact_mod
import kohakuterrarium.core.agent_model as agent_model_mod
import kohakuterrarium.llm.api_keys as api_keys_mod
import kohakuterrarium.llm.anthropic_provider  # noqa: F401
import kohakuterrarium.llm.backends as backends_mod
import kohakuterrarium.llm.codex_auth as codex_auth_mod
import kohakuterrarium.llm.codex_provider  # noqa: F401
import kohakuterrarium.llm.openai  # noqa: F401
import kohakuterrarium.studio.sessions.summary.tracking as summary_mod
from kohakuterrarium.llm.base import BaseLLMProvider
from kohakuterrarium.testing.llm import ScriptedLLM

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


class RealProviderBlocked(RuntimeError):
    """A test tried to build a real LLM provider."""


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


def _blocked_provider_init(self, *args, **kwargs):
    raise RealProviderBlocked(
        f"{type(self).__name__} is a real LLM provider; tests must use ScriptedLLM"
    )


def _provider_classes() -> list[type]:
    """Return BaseLLMProvider and every subclass loaded so far."""
    found, todo = [], [BaseLLMProvider]
    while todo:
        cls = todo.pop()
        found.append(cls)
        todo.extend(cls.__subclasses__())
    return found


def _scripted(*args, **kwargs):
    return ScriptedLLM(["(scripted)"])


FACTORIES = (
    (bootstrap_llm_mod, "create_llm_provider"),
    (agent_init_mod, "create_llm_provider"),
    (bootstrap_llm_mod, "create_llm_from_profile_name"),
    (agent_model_mod, "create_llm_from_profile_name"),
    (agent_compact_mod, "create_llm_from_profile_name"),
    (summary_mod, "create_llm_from_profile_name"),
)


def install(monkeypatch, tmp_path: Path, *, network: bool = True) -> None:
    """Patch every factory, credential path and the network for one test."""
    for module, name in FACTORIES:
        monkeypatch.setattr(module, name, _scripted)
    for cls in _provider_classes():
        monkeypatch.setattr(cls, "__init__", _blocked_provider_init)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setattr(
        codex_auth_mod, "CODEX_CLI_TOKEN_PATH", home / ".codex" / "auth.json"
    )
    monkeypatch.setattr(
        codex_auth_mod,
        "DEFAULT_TOKEN_PATH",
        home / ".kohakuterrarium" / "codex-auth.json",
    )
    monkeypatch.setattr(api_keys_mod, "KT_DIR", home / ".kohakuterrarium")
    monkeypatch.setattr(
        backends_mod, "PROFILES_PATH", home / ".kohakuterrarium" / "llm_profiles.yaml"
    )
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
