"""The reasons ``qq_send`` reports: what happened, then the next step to take."""

from kt_qqbot.errors import QQApiError

LINK_REFUSED_CODE = 40054010
MEDIA_ACCEPTED = (
    "images (png/jpg/gif/webp), mp4 video, silk voice, and files in private chats"
)

UNBOUND = "the bot is unbound. Stop sending; the operator must run kt-qqbot login."
CLOSED_WINDOW = (
    "the reply window has closed (group: someone must speak within 5 min; "
    "private: 60 min). Wait for the next message, then send."
)
DUPLICATE_SEQ = (
    "QQ saw this reply number twice. Send the same message once more; "
    "if it fails again, wait for the next message."
)


def reply_target_missing(seq: int, low: int) -> str:
    """Reason for a ``reply_to`` tag that is not in the chat's stored log."""
    if seq < low:
        return (
            f"m{seq} is older than the stored log (it starts at m{low}). "
            "Send again without reply_to."
        )
    return f"m{seq} is not in this chat. Send again without reply_to."


def api_failure(exc: QQApiError, *, upload: bool) -> str:
    """Reason for a QQ OpenAPI error during a media upload or a message send."""
    detail = exc.message or str(exc)
    if exc.status == 0:
        return f"could not reach QQ ({detail}). Try once more in a minute."
    if exc.status >= 500:
        return f"QQ had a server error ({detail}). Try once more in a minute."
    if upload:
        return (
            f"QQ refused the media ({detail}). Send a different file; QQ takes "
            f"{MEDIA_ACCEPTED}."
        )
    if exc.code == LINK_REFUSED_CODE:
        return (
            f"QQ refused the message because it has a link ({detail}). "
            "Send it again without the link."
        )
    return (
        f"QQ refused the message (code {exc.code}: {detail}). Rephrase it within "
        "the content rules and send once, or stay silent; do not resend the same text."
    )
