import json
import subprocess

import httpx
import pytest
import respx
import tomlkit
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from marvin_mcp import setup
from marvin_mcp.setup import Server, SetupError

SERVER = Server("/bin/uvx", ["--refresh", "--from", setup.SOURCE, "marvin-mcp"], {"MARVIN_API_KEY": "ak_1"})


@respx.mock
@pytest.mark.parametrize(
    "status, message",
    [(401, "not accepted"), (404, "private alpha version. Please email marvin@marvin-labs.com")],
)
def test_check_key_explains_a_rejected_key(status, message):
    respx.get("https://marvin.test/v1/conversations").mock(return_value=httpx.Response(status))

    with pytest.raises(SetupError, match=message):
        setup.check_key("ak_1", "https://marvin.test")


@pytest.mark.parametrize(
    "project, scope", [(True, "local"), (False, "user")], ids=["project", "global"]
)
def test_claude_code_uses_a_private_scope_for_the_project(monkeypatch, tmp_path, project, scope):
    calls = []
    monkeypatch.setattr(setup.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw.get("cwd"))))

    setup.install_claude_code(SERVER, tmp_path if project else None)

    add, cwd = calls[-1]
    assert add[:4] == ["claude", "mcp", "add-json", "marvin"]
    assert add[-2:] == ["-s", scope]
    assert json.loads(add[4])["env"] == {"MARVIN_API_KEY": "ak_1"}
    assert cwd == (tmp_path if project else None)


def _git_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def test_codex_project_config_keeps_other_settings_and_stays_out_of_git(tmp_path):
    _git_repo(tmp_path)
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "o3"  # mine\n\n[mcp_servers.other]\ncommand = "x"\n')

    setup.install_codex(SERVER, tmp_path)
    setup.install_codex(SERVER, tmp_path)  # a second run replaces, not duplicates

    text = config.read_text()
    assert '# mine' in text
    parsed = tomlkit.parse(text)
    assert set(parsed["mcp_servers"]) == {"other", "marvin"}
    assert parsed["mcp_servers"]["marvin"]["env"]["MARVIN_API_KEY"] == "ak_1"
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count("/.codex/config.toml") == 1


def test_codex_global_config_is_written_without_a_command_line(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('model = "o3"\n')
    (tmp_path / "config.toml").chmod(0o644)
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: pytest.fail("ran a command"))

    setup.install_codex(SERVER, None)

    parsed = tomlkit.parse((tmp_path / "config.toml").read_text())
    assert parsed["model"] == "o3"
    assert parsed["mcp_servers"]["marvin"]["env"]["MARVIN_API_KEY"] == "ak_1"
    assert (tmp_path / "config.toml").stat().st_mode & 0o777 == 0o600  # holds the key


def test_a_symlinked_config_is_written_through(tmp_path):
    real = tmp_path / "dotfiles" / "config.toml"
    real.parent.mkdir()
    real.write_text('model = "o3"\n')
    link = tmp_path / "config.toml"
    link.symlink_to(real)

    setup.write_codex_config(SERVER, link)

    assert link.is_symlink()
    assert "marvin" in real.read_text()


def test_codex_refuses_a_committed_project_config(tmp_path):
    _git_repo(tmp_path)
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "o3"\n')
    subprocess.run(["git", "add", ".codex/config.toml"], cwd=tmp_path, check=True)

    with pytest.raises(SetupError, match="committed to git"):
        setup.install_codex(SERVER, tmp_path)
    assert "marvin" not in config.read_text()


def test_claude_desktop_keeps_the_other_servers(tmp_path):
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))

    setup.install_claude_desktop(SERVER, config)

    data = json.loads(config.read_text())
    assert data["theme"] == "dark"
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["marvin"]["command"] == "/bin/uvx"
    assert config.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "keys, expected",
    [
        ("\r", [0, 1]),  # enter confirms the defaults
        ("\x1b[B\x1b[B \r", [0, 1, 2]),  # down, down, space ticks the third
        (" \r", [1]),  # space unticks the first
        ("\x03", None),  # ctrl-c cancels
    ],
    ids=["enter", "tick", "untick", "cancel"],
)
def test_pick_is_driven_by_the_keyboard(keys, expected):
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(keys)
        assert setup.pick("Add Marvin to:", [(0, "a"), (1, "b"), (2, "c")], [0, 1]) == expected
