"""``kt-qqbot`` command line: login (QR or manual), logout, status."""

import argparse
import asyncio
import getpass
import sys
from typing import TextIO

import qrcode

from kt_qqbot.api import check_credentials
from kt_qqbot.auth.bind import bind_flow
from kt_qqbot.auth.credentials import Credentials, CredentialStore
from kt_qqbot.errors import QQBotError


def print_qr(url: str, out: TextIO | None = None) -> None:
    """Print ``url`` as a terminal QR code followed by the plain link."""
    out = out or sys.stdout
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(out=out, invert=True)
    print(f"\nScan with mobile QQ, or open: {url}\n", file=out)


async def _login_qr(timeout: float | None) -> int:
    async for step in bind_flow(timeout=timeout):
        match step.kind:
            case "show_qr":
                print_qr(step.url)
                print("Waiting for confirmation in QQ… (Ctrl-C to cancel)")
            case "progress":
                print(step.message)
            case "done":
                print(step.message)
                return 0
            case "error":
                print(f"Binding failed: {step.message}", file=sys.stderr)
                return 1
    return 1


async def _check(creds: Credentials) -> None:
    await check_credentials(creds)


def _login_manual() -> int:
    app_id = input("AppID: ").strip()
    secret = getpass.getpass("AppSecret: ").strip()
    if not app_id or not secret:
        print("AppID and AppSecret are both required.", file=sys.stderr)
        return 1
    creds = Credentials(app_id, secret)
    try:
        asyncio.run(_check(creds))
    except QQBotError as exc:
        print(f"Not saved: {exc}", file=sys.stderr)
        return 1
    CredentialStore().save(creds)
    print(f"Saved bot {app_id}.")
    return 0


def _status() -> int:
    store = CredentialStore()
    ids = store.list_app_ids()
    try:
        creds = store.load()
    except QQBotError as exc:
        print(str(exc))
        return 1
    default = store.default_app_id()
    print(f"Stored bots: {', '.join(ids) or '(none)'}; default: {default or '-'}")
    print(f"Checking bot {creds.app_id}…")
    try:
        asyncio.run(_check(creds))
    except QQBotError as exc:
        print(f"Token check failed: {exc}", file=sys.stderr)
        return 1
    print("Token check OK.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the ``kt-qqbot`` command."""
    parser = argparse.ArgumentParser(prog="kt-qqbot")
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login", help="bind a bot by QR code, or enter it by hand")
    login.add_argument("--manual", action="store_true", help="enter AppID / AppSecret")
    login.add_argument("--timeout", type=float, default=None, help="seconds to wait")
    logout = sub.add_parser("logout", help="forget a bot's credentials")
    logout.add_argument("app_id", nargs="?", default=None)
    sub.add_parser("status", help="show stored bots and check the default one")
    args = parser.parse_args(argv)

    match args.command:
        case "login":
            if args.manual:
                return _login_manual()
            try:
                return asyncio.run(_login_qr(args.timeout))
            except KeyboardInterrupt:
                print("Cancelled.")
                return 130
        case "logout":
            removed = CredentialStore().remove(args.app_id)
            print(f"Removed bot {removed}." if removed else "No such bot stored.")
            return 0 if removed else 1
        case "status":
            return _status()
    return 2


if __name__ == "__main__":
    sys.exit(main())
