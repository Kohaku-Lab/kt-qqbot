"""Package-wide UI owner behind the dashboard's "QQ Bot" settings section.

One instance per host process (``ui.handler``). It reports bound bots and
their gateway state, runs the QR bind flow and pushes each step, and
handles manual entry, token tests and unbinding. Secrets never leave it.
"""

import asyncio
import os
from dataclasses import asdict
from typing import Any

from kohakuterrarium.modules.ui_owner import UIOwnerMixin
from kohakuterrarium.utils.logging import get_logger

from kt_qqbot import api as qq_api
from kt_qqbot import hub as hub_registry
from kt_qqbot.auth import bind as qq_bind
from kt_qqbot.auth.credentials import ENV_APP_ID, Credentials, CredentialStore
from kt_qqbot.errors import AdminRequired, BadOwnerArgument, QQBotError

logger = get_logger("kt_qqbot.ui_owner")

ADMIN_ACTIONS = {"bind_start", "bind_cancel", "save_manual", "unbind"}
MAX_ID_CHARS = 64
MAX_SECRET_CHARS = 256


MAX_VIEWERS = 64


def _multi_user(context: Any) -> bool:
    return bool(getattr(context, "multi_user", False))


def require_admin(context: Any) -> None:
    """Allow single-user hosts and admins; refuse everyone else under multi-user."""
    if not _multi_user(context) or getattr(context, "is_admin", False):
        return
    raise AdminRequired("only an admin can change QQ bot credentials")


class QQBotUI(UIOwnerMixin):
    """Bind status, QR bind flow, manual entry, token test, unbind."""

    def __init__(self, store: CredentialStore | None = None):
        self._store = store
        self._task: asyncio.Task | None = None
        self._bind: dict[str, str] = {"phase": "idle", "url": "", "message": ""}
        self._bind_user: int | None = None
        self._viewers: dict[int, Any] = {}
        self._multi = False

    @property
    def store(self) -> CredentialStore:
        """Return the credential store (resolved lazily so config-dir changes apply)."""
        return self._store or CredentialStore()

    # ── state ──────────────────────────────────────────────────────

    def ui_state(self, context: Any = None) -> dict:
        """Return bots, gateway states and the bind flow; never a secret.

        Under multi-user mode only admins and the user who started a bind see
        its QR link.
        """
        self._note_viewer(context)
        store = self.store
        hubs = hub_registry.running_hubs()
        default = store.default_app_id()
        bots = []
        for app_id in store.list_app_ids():
            hub = hubs.get(app_id)
            bots.append(
                {
                    "app_id": app_id,
                    "name": hub.bot_identity.get("username", "") if hub else "",
                    "source": "file",
                    "default": app_id == default,
                    "gateway": hub.status if hub else "not running",
                }
            )
        env_id = os.environ.get(ENV_APP_ID, "").strip()
        if env_id and env_id not in {b["app_id"] for b in bots}:
            hub = hubs.get(env_id)
            bots.append(
                {
                    "app_id": env_id,
                    "name": hub.bot_identity.get("username", "") if hub else "",
                    "source": "env",
                    "default": not default,
                    "gateway": hub.status if hub else "not running",
                }
            )
        bind = dict(self._bind)
        if not self._sees_bind_url(context):
            bind["url"] = ""
        return {"bots": bots, "default": default, "bind": bind}

    def _sees_bind_url(self, context: Any) -> bool:
        if not _multi_user(context) or getattr(context, "is_admin", False):
            return True
        user_id = getattr(context, "user_id", None)
        return user_id is not None and user_id == self._bind_user

    def _note_viewer(self, context: Any) -> None:
        if not _multi_user(context):
            return
        self._multi = True
        user_id = getattr(context, "user_id", None)
        if user_id is None:
            return
        self._viewers.pop(user_id, None)
        self._viewers[user_id] = context
        while len(self._viewers) > MAX_VIEWERS:
            self._viewers.pop(next(iter(self._viewers)))

    def _push(self) -> None:
        """Push state: one untargeted push, or one per signed-in viewer under L4."""
        if not self._multi:
            self.ui_emit("state", self.ui_state())
            return
        for user_id, context in list(self._viewers.items()):
            self.ui_emit("state", self.ui_state(context), user_id=user_id)

    # ── actions ────────────────────────────────────────────────────

    async def ui_call(self, action: str, args: dict, context: Any = None) -> Any:
        """Run one settings action; admin-only actions check ``context``."""
        self._note_viewer(context)
        if action in ADMIN_ACTIONS:
            require_admin(context)
        match action:
            case "refresh":
                return self.ui_state(context)
            case "bind_start":
                return await self.bind_start(getattr(context, "user_id", None))
            case "bind_cancel":
                return await self.bind_cancel()
            case "save_manual":
                return await self.save_manual(args)
            case "test":
                return await self.test(args)
            case "unbind":
                return await self.unbind(args)
        raise BadOwnerArgument(f"unknown action {action!r}")

    async def bind_start(self, user_id: int | None = None) -> dict:
        """Start (or restart) the QR bind flow; its steps go to ``user_id`` only."""
        if self._task is not None:
            await self._cancel_task()
            replaced = {"kind": "cancelled", "message": "Binding restarted."}
            self.ui_emit("bind", replaced, user_id=self._bind_user)
        self._bind_user = user_id
        self._bind = {"phase": "starting", "url": "", "message": ""}
        self._push()
        self._task = asyncio.create_task(self._run_bind(user_id))
        return {"ok": True}

    async def bind_cancel(self) -> dict:
        """Stop a running bind flow."""
        if self._task is None:
            return {"ok": False, "message": "no bind in progress"}
        target = self._bind_user
        await self._cancel_task()
        self._bind = {"phase": "cancelled", "url": "", "message": "Binding cancelled."}
        self._bind_user = None
        cancelled = {"kind": "cancelled", "message": "Binding cancelled."}
        self.ui_emit("bind", cancelled, user_id=target)
        self._push()
        return {"ok": True}

    async def aclose(self) -> None:
        """Cancel a running bind flow when the handler is replaced or unloaded."""
        await self._cancel_task()

    async def _cancel_task(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run_bind(self, user_id: int | None) -> None:
        try:
            async for step in qq_bind.bind_flow(store=self.store):
                self._apply_step(step)
                if step.kind == "done":
                    await hub_registry.apply_credentials(step.app_id)
                self.ui_emit("bind", asdict(step), user_id=user_id)
                self._push()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("QQ bind flow failed", error=str(exc), exc_info=True)
            self._bind = {"phase": "error", "url": "", "message": str(exc)}
            self.ui_emit(
                "bind", {"kind": "error", "message": str(exc)}, user_id=user_id
            )
            self._push()
        finally:
            if self._task is asyncio.current_task():
                self._task = None

    def _apply_step(self, step: Any) -> None:
        match step.kind:
            case "show_qr":
                self._bind = {"phase": "waiting", "url": step.url, "message": ""}
            case "progress":
                self._bind = dict(self._bind, message=step.message)
            case "done":
                self._bind = {
                    "phase": "done",
                    "url": "",
                    "message": step.message,
                    "app_id": step.app_id,
                }
            case "error":
                self._bind = {"phase": "error", "url": "", "message": step.message}

    async def save_manual(self, args: dict) -> dict:
        """Check then store an AppID / AppSecret typed into the form."""
        app_id = str(args.get("app_id") or "").strip()
        secret = str(args.get("app_secret") or "").strip()
        if not app_id or not secret:
            raise BadOwnerArgument("app_id and app_secret are both required")
        if len(app_id) > MAX_ID_CHARS or len(secret) > MAX_SECRET_CHARS:
            raise BadOwnerArgument("app_id or app_secret is too long")
        creds = Credentials(app_id, secret)
        try:
            await qq_api.check_credentials(creds)
        except QQBotError as exc:
            return {"ok": False, "message": str(exc)}
        self.store.save(creds, make_default=bool(args.get("make_default", True)))
        await hub_registry.apply_credentials(app_id)
        self._push()
        return {"ok": True, "app_id": app_id}

    async def test(self, args: dict) -> dict:
        """Fetch one token for a stored bot."""
        app_id = str(args.get("app_id") or "").strip() or None
        try:
            creds = self.store.load(app_id)
            await qq_api.check_credentials(creds)
        except QQBotError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "app_id": creds.app_id, "message": "Token check OK."}

    async def unbind(self, args: dict) -> dict:
        """Forget a stored bot and stop its running hub."""
        app_id = str(args.get("app_id") or "").strip() or None
        removed = self.store.remove(app_id)
        if removed is None:
            return {"ok": False, "message": "no such bot stored"}
        await hub_registry.apply_credentials(removed)
        self._push()
        return {"ok": True, "app_id": removed}
