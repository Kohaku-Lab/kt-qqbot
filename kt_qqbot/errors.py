"""Typed errors raised by the QQ bot package."""

from kohakuterrarium.errors import KTError
from kohakuterrarium.modules.ui_owner import OwnerArgumentError, OwnerForbiddenError


class QQBotError(KTError, RuntimeError):
    """Base class for every kt-qqbot failure."""


class CredentialsMissing(QQBotError):
    """No bot credentials are configured for the requested AppID."""

    def __init__(self, app_id: str | None = None):
        target = f"bot {app_id}" if app_id else "any bot"
        super().__init__(
            f"No QQ bot credentials for {target}. "
            "Run `kt-qqbot login` (or `kt-qqbot login --manual`), "
            "or set QQBOT_APPID and QQBOT_SECRET."
        )
        self.app_id = app_id


class AuthFailed(QQBotError):
    """The QQ platform rejected the configured AppID / AppSecret."""

    def __init__(self, app_id: str, detail: str):
        super().__init__(
            f"QQ rejected the credentials of bot {app_id}: {detail}. "
            "Run `kt-qqbot login` to bind again."
        )
        self.app_id = app_id


class QQApiError(QQBotError):
    """A QQ OpenAPI call returned an error."""

    def __init__(self, status: int, code: int | None, message: str):
        super().__init__(f"QQ API error (HTTP {status}, code {code}): {message}")
        self.status = status
        self.code = code
        self.message = message


class GatewayFatal(QQBotError):
    """The gateway closed with a code that reconnecting cannot fix."""

    def __init__(self, code: int, reason: str):
        super().__init__(f"QQ gateway closed fatally ({code}): {reason}")
        self.code = code


class BindError(QQBotError):
    """The QR bind protocol failed."""


class MediaError(QQBotError):
    """An outbound media item cannot be sent as given."""


class BadOwnerArgument(QQBotError, OwnerArgumentError):
    """A dashboard action got arguments it cannot use."""


class AdminRequired(QQBotError, OwnerForbiddenError):
    """The action needs an admin user."""


class BotInUse(QQBotError):
    """Another process already runs this bot."""

    def __init__(self, app_id: str, detail: str = ""):
        super().__init__(
            f"QQ bot {app_id} is already running in another process"
            + (f" ({detail})" if detail else "")
            + "; stop it first, one process per bot."
        )
        self.app_id = app_id
