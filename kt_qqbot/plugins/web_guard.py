"""``qq_web_guard`` plugin: refuse ``web_fetch`` to non-public addresses.

QQ users can steer the creature, so a fetch of ``http://192.168.1.1/`` must
not reach the operator's network. The host is resolved once here; redirects
followed by the fetch backends are not re-checked.
"""

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

from kohakuterrarium.modules.plugin.base import BasePlugin, PluginBlockError

GUARDED_TOOLS = {"web_fetch": "url"}


def _global(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_multicast


class QQWebGuardPlugin(BasePlugin):
    """Block web fetches whose host is loopback, private, link-local or otherwise not global."""

    name = "qq_web_guard"
    priority = 10
    description = "Refuse web_fetch to private or local addresses."

    def __init__(self, tools: dict[str, str] | None = None):
        super().__init__()
        self.tools = dict(GUARDED_TOOLS if tools is None else tools)

    async def _check_host(self, host: str) -> None:
        try:
            if not _global(host):
                raise PluginBlockError(f"web_fetch: {host} is a non-public address")
            return
        except ValueError:
            pass
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise PluginBlockError(f"web_fetch: cannot resolve {host}: {exc}") from exc
        if not infos or not all(_global(info[4][0]) for info in infos):
            raise PluginBlockError(
                f"web_fetch: {host} resolves to a non-public address"
            )

    async def pre_tool_execute(self, args: dict, **kwargs: Any) -> dict | None:
        """Check the URL argument of a guarded tool."""
        arg = self.tools.get(str(kwargs.get("tool_name") or ""))
        if not arg or not args.get(arg):
            return None
        url = str(args[arg]).strip()
        if "://" not in url:
            url = "https://" + url
        host = urlparse(url).hostname or ""
        if not host:
            raise PluginBlockError("web_fetch: the URL has no host")
        await self._check_host(host.strip("[]"))
        return None
