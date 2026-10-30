"""The qq_character plugin and the kt-qqbot command line."""

import base64
import json

import httpx

from kt_qqbot import cli
from kt_qqbot.auth import bind
from kt_qqbot.auth.credentials import Credentials, CredentialStore
from kt_qqbot.auth.crypto import encrypt_secret
from kt_qqbot.paths import root_dir
from kt_qqbot.plugins.character import QQCharacterPlugin

from tests.conftest import SECRET


class TestCharacterPlugin:
    def test_reads_main_then_extras_from_config_dir(self):
        plugin = QQCharacterPlugin()
        assert plugin.get_prompt_content(None) is None
        base = root_dir()
        (base / "character.md").write_text("I am Kohaku.", encoding="utf-8")
        (base / "character").mkdir()
        (base / "character" / "b_style.md").write_text("Short replies.")
        (base / "character" / "a_likes.md").write_text("Likes tea.")
        (base / "character" / "empty.md").write_text("   ")
        (base / "character" / "notes.txt").write_text("ignored")
        content = plugin.get_prompt_content(None)
        assert content.startswith("## Character\n\nI am Kohaku.")
        assert content.index("Likes tea.") < content.index("Short replies.")
        assert "ignored" not in content

    def test_directory_option_and_truncation(self, tmp_path):
        (tmp_path / "character.md").write_text("x" * 50)
        plugin = QQCharacterPlugin(
            directory=str(tmp_path), heading="Persona", max_chars=10
        )
        assert plugin.get_prompt_content(None) == "## Persona\n\n" + "x" * 10


def _bind_transport():
    keys = []

    def handler(request):
        body = json.loads(request.content)
        if request.url.path == "/lite/create_bind_task":
            keys.append(base64.b64decode(body["key"]))
            return httpx.Response(200, json={"retcode": 0, "data": {"task_id": "T"}})
        data = {
            "status": 2,
            "bot_appid": "555",
            "bot_encrypt_secret": encrypt_secret("SEC", keys[-1]),
        }
        return httpx.Response(200, json={"retcode": 0, "data": data})

    return httpx.MockTransport(handler)


class TestCli:
    def test_login_qr_prints_qr_and_saves(self, monkeypatch, capsys):
        real_client = httpx.AsyncClient
        transport = _bind_transport()
        monkeypatch.setattr(bind, "POLL_INTERVAL_S", 0)
        monkeypatch.setattr(
            bind.httpx, "AsyncClient", lambda: real_client(transport=transport)
        )
        assert cli.main(["login"]) == 0
        out = capsys.readouterr().out
        assert "connect.html?task_id=T" in out and "Bound bot 555" in out
        assert "SEC" not in out
        assert CredentialStore().load("555").app_secret == "SEC"

    def test_status_logout_and_missing(self, monkeypatch, capsys, fake_api):
        assert cli.main(["status"]) == 1
        assert "kt-qqbot login" in capsys.readouterr().out
        CredentialStore().save(Credentials("1", SECRET))

        async def fake_check(creds):
            return None

        monkeypatch.setattr(cli, "_check", fake_check)
        assert cli.main(["status"]) == 0
        assert "Token check OK" in capsys.readouterr().out
        assert cli.main(["logout"]) == 0
        assert cli.main(["logout"]) == 1

    def test_print_qr_includes_link(self, capsys):
        cli.print_qr("https://q.qq.com/x")
        out = capsys.readouterr().out
        assert "https://q.qq.com/x" in out and "▀" in out
