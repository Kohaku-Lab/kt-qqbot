"""The test guard itself: real providers, real credentials and the network are blocked."""

import importlib
import socket

import httpx
import pytest

import kohakuterrarium.bootstrap.llm as bootstrap_llm_mod
import kohakuterrarium.core.agent.history.compact as agent_compact_mod
import kohakuterrarium.core.agent.model as agent_model_mod
from kohakuterrarium.llm.codex.auth import CodexTokens
from kohakuterrarium.llm.codex.provider import CodexOAuthProvider
from kohakuterrarium.llm.openai.provider import OpenAIProvider
from kohakuterrarium.testing.llm import ScriptedLLM
from kohakuterrarium.testing.llm_seam import LLM_FACTORY_TARGETS

from tests import safety
from tests.safety import REAL_HOME, GuardViolation, RealProviderBlocked


def test_real_providers_cannot_be_built():
    with pytest.raises(RealProviderBlocked):
        CodexOAuthProvider()
    with pytest.raises(RealProviderBlocked):
        OpenAIProvider()


def test_every_factory_is_scripted():
    assert isinstance(
        bootstrap_llm_mod.create_llm_from_profile_name("codex/gpt-6"), ScriptedLLM
    )
    assert isinstance(agent_compact_mod.create_llm_from_profile_name("x"), ScriptedLLM)
    assert isinstance(agent_model_mod.create_llm_from_profile_name("x"), ScriptedLLM)
    assert isinstance(bootstrap_llm_mod.create_llm_provider(None), ScriptedLLM)


def test_every_core_factory_target_is_replaced():
    for module_name, attr in LLM_FACTORY_TARGETS:
        factory = getattr(importlib.import_module(module_name), attr)
        assert isinstance(factory("any"), ScriptedLLM), (module_name, attr)


def test_real_credentials_are_unreadable():
    with pytest.raises(GuardViolation):
        open(REAL_HOME / ".codex" / "auth.json")
    with pytest.raises(GuardViolation):
        (REAL_HOME / ".kohakuterrarium" / "llm_profiles.yaml").read_text()
    assert CodexTokens.load() is None, "token search stays in tmp"


def test_credential_check_is_the_guard_not_the_filesystem():
    probe = REAL_HOME / ".codex" / "guard-probe-does-not-exist.json"
    with pytest.raises(GuardViolation):
        open(probe)
    safety._active["on"] = False
    try:
        with pytest.raises(FileNotFoundError):
            open(probe)
    finally:
        safety._active["on"] = True


async def test_network_is_blocked():
    with pytest.raises((GuardViolation, socket.gaierror)):
        socket.create_connection(("1.1.1.1", 443), timeout=1)
    raw = socket.socket()
    with pytest.raises(GuardViolation):
        raw.connect(("1.1.1.1", 443))
    raw.close()
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPError):
            await client.get("https://auth.openai.com/oauth/token")
    socket.getaddrinfo("localhost", 80)
