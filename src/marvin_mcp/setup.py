"""``marvin-mcp setup``: ask for an API key, check it, and register the server in the AI clients found.

By default the server is added for the current project only; ``--global`` adds it for every
project and is the only way to reach the desktop apps, which have no per-project setting.

| Client | Project (default) | Global (``--global``) |
|---|---|---|
| Claude Code | ``claude mcp add-json -s local``: this project, kept out of the repo | ``-s user`` |
| Codex | ``.codex/config.toml`` in the project, excluded from git | ``~/.codex/config.toml`` |
| Claude Desktop | (none) | its ``claude_desktop_config.json`` |
| ChatGPT desktop | (none) | prints what to paste into its settings |

Claude Code's ``project`` scope is deliberately not used: it writes ``.mcp.json``, which is meant to
be committed, and the entry carries the API key. Running setup again replaces the existing
``marvin`` entry, so it also rotates a key.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import tomlkit
from prompt_toolkit import Application, prompt
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.layout import FormattedTextControl, HSplit, Layout, Window
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import CheckboxList
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from marvin_mcp.server import ACCESS_MESSAGE, DEFAULT_API_URL

SERVER_NAME = "marvin"
SOURCE = "git+https://github.com/Marvin-Labs/marvin-mcp"


class SetupError(Exception):
    """A problem the user has to fix; printed without a traceback."""


@dataclass(frozen=True)
class Server:
    command: str
    args: list[str]
    env: dict[str, str]

    def as_dict(self) -> dict:
        return {"command": self.command, "args": self.args, "env": self.env}


def server_for(api_key: str, api_url: str, source: str = SOURCE) -> Server:
    # The absolute path, because GUI apps (Claude Desktop on macOS) do not see the shell's PATH.
    uvx = shutil.which("uvx") or "uvx"
    env = {"MARVIN_API_KEY": api_key}
    if api_url != DEFAULT_API_URL:
        env["MARVIN_API_URL"] = api_url
    return Server(uvx, ["--refresh", "--from", source, "marvin-mcp"], env)


def check_key(api_key: str, api_url: str) -> None:
    try:
        response = httpx.get(
            f"{api_url.rstrip('/')}/v1/conversations",
            params={"limit": 1},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise SetupError(f"Could not reach the Marvin API at {api_url}: {exc}") from exc
    if response.status_code == 401:
        raise SetupError("That API key was not accepted. Check it and try again.")
    if response.status_code == 404:
        raise SetupError(ACCESS_MESSAGE)
    if response.is_error:
        raise SetupError(f"The Marvin API answered {response.status_code}; try again later.")


def write_private(path: Path, text: str) -> None:
    """Replace ``path`` atomically with a file only its owner can read: it holds the API key.

    A symlinked config (dotfiles) is written through, not replaced by a regular file.
    """
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.chmod(tmp, 0o600)  # O_CREAT's mode does not apply to a leftover tmp file
    tmp.replace(path)


# --- Claude Code -----------------------------------------------------------------------------


def install_claude_code(server: Server, project: Path | None) -> str:
    scope, cwd = ("local", project) if project else ("user", None)
    # add-json takes the entry only as an argument, so the key is briefly on the command line, as
    # with Claude's own `claude mcp add --env`. It refuses an existing name: remove the old one first.
    removed = subprocess.run(
        ["claude", "mcp", "remove", SERVER_NAME, "-s", scope], cwd=cwd, capture_output=True
    )
    entry = json.dumps({"type": "stdio", **server.as_dict()})
    try:
        subprocess.run(
            ["claude", "mcp", "add-json", SERVER_NAME, entry, "-s", scope],
            cwd=cwd,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        if removed.returncode == 0:
            detail = (exc.stderr or b"").decode(errors="replace").strip()
            raise SetupError(
                f"{detail or exc}. Your previous Marvin entry was removed; run setup again."
            ) from exc
        raise
    return f"added for {project}" if project else "added for all projects"


# --- Codex -----------------------------------------------------------------------------------


def _git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=project, capture_output=True, text=True)


def keep_out_of_git(project: Path, path: Path) -> None:
    """Exclude ``path`` locally; refuse if git already tracks it, since it now holds the key."""
    top = _git(project, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return  # not a git repository
    root = Path(top.stdout.strip())
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    if _git(root, "ls-files", "--error-unmatch", relative).returncode == 0:
        raise SetupError(
            f"{relative} is committed to git, and the Marvin entry would put your API key in it. "
            "Run setup with --global instead."
        )
    exclude = Path(_git(root, "rev-parse", "--git-path", "info/exclude").stdout.strip())
    exclude = exclude if exclude.is_absolute() else root / exclude
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if f"/{relative}" not in lines:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("\n".join([*lines, f"/{relative}"]) + "\n")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def write_codex_config(server: Server, config: Path) -> None:
    """Set the ``marvin`` entry in a Codex config, keeping everything else in it.

    Written directly rather than through ``codex mcp add``, so the key is never on a command line.
    """
    document = tomlkit.parse(config.read_text()) if config.exists() else tomlkit.document()
    servers = document.setdefault("mcp_servers", tomlkit.table(is_super_table=True))
    servers[SERVER_NAME] = server.as_dict()
    write_private(config, tomlkit.dumps(document))


def install_codex(server: Server, project: Path | None) -> str:
    if project is None:
        config = codex_home() / "config.toml"
        write_codex_config(server, config)
        return f"added to {config} for all projects"
    config = project / ".codex" / "config.toml"
    keep_out_of_git(project, config)
    write_codex_config(server, config)
    return f"added to {config} (Codex only loads it once you trust this project)"


# --- Claude Desktop and ChatGPT ----------------------------------------------------------------


def claude_desktop_config() -> Path | None:
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    elif system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    folder = base / "Claude"
    return folder / "claude_desktop_config.json" if folder.is_dir() else None


def install_claude_desktop(server: Server, config: Path) -> str:
    text = config.read_text() if config.exists() else ""
    data = json.loads(text) if text.strip() else {}
    data.setdefault("mcpServers", {})[SERVER_NAME] = server.as_dict()
    write_private(config, json.dumps(data, indent=2) + "\n")
    return "added"


def chatgpt_instructions(server: Server) -> str:
    env = "\n".join(f"  {key}={value}" for key, value in server.env.items())
    return (
        "Add a local MCP server in ChatGPT's settings with:\n\n"
        f"name:        {SERVER_NAME}\n"
        f"command:     {server.command}\n"
        f"arguments:   {' '.join(server.args)}\n"
        f"environment:\n{env}"
    )


# --- Command -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    label: str
    install: Callable[[Server], str]
    default: bool = True
    instructions: bool = False  # install returns text to show, not a status line


def targets(project: Path | None) -> list[Target]:
    found: list[Target] = []
    if shutil.which("claude"):
        found.append(Target("Claude Code", lambda s: install_claude_code(s, project)))
    if shutil.which("codex") or (project and (project / ".codex").is_dir()):
        found.append(Target("Codex (CLI and app)", lambda s: install_codex(s, project)))
    if project:
        return found
    desktop = claude_desktop_config()
    if desktop:
        found.append(Target("Claude Desktop", lambda s: install_claude_desktop(s, desktop)))
    found.append(Target("ChatGPT desktop", chatgpt_instructions, default=False, instructions=True))
    return found


def pick(message: str, options: list[tuple[int, str]], checked: list[int]) -> list[int] | None:
    """An inline multi-select under the prompt; ``None`` when cancelled."""
    checkboxes = CheckboxList(values=options, default_values=checked, select_character="x")
    keys = KeyBindings()

    @keys.add("enter")
    def _done(event) -> None:
        event.app.exit(result=list(checkboxes.current_values))

    @keys.add("c-c")
    def _cancel(event) -> None:
        event.app.exit(result=None)

    # The list binds enter to "toggle" on itself, which outranks app-level bindings; the
    # later bindings in a merge win, so enter confirms instead.
    control = checkboxes.control
    control.key_bindings = merge_key_bindings([control.key_bindings or KeyBindings(), keys])

    hint = Window(
        FormattedTextControl([("class:hint", "  ↑/↓ move · space toggles · enter confirms")]),
        height=1,
    )
    body = HSplit([Window(FormattedTextControl(message), height=1), checkboxes, hint])
    app: Application[list[int] | None] = Application(
        layout=Layout(body, focused_element=checkboxes),
        style=Style.from_dict({"hint": "#888888", "checkbox-selected": "bold"}),
        full_screen=False,
        erase_when_done=True,
    )
    return app.run()


def choose(found: list[Target], ask: bool) -> list[Target] | None:
    """The targets to set up; ``None`` when the user cancelled."""
    if not ask:
        return [t for t in found if t.default]
    picked = pick(
        "Add Marvin to:",
        [(i, t.label) for i, t in enumerate(found)],
        [i for i, t in enumerate(found) if t.default],
    )
    return None if picked is None else [found[i] for i in sorted(picked)]


def run(argv: list[str], console: Console) -> int:
    parser = argparse.ArgumentParser(
        prog="marvin-mcp setup",
        description="Add the Marvin MCP server to Claude Code and Codex for this project, or with "
        "--global to every project plus Claude Desktop and ChatGPT.",
    )
    parser.add_argument(
        "-g", "--global", dest="global_", action="store_true",
        help="Add it for all projects, and to Claude Desktop and ChatGPT desktop",
    )
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="Project directory (default: here)")
    parser.add_argument("--api-url", default=os.environ.get("MARVIN_API_URL", DEFAULT_API_URL))
    parser.add_argument(
        "--source", default=SOURCE,
        help="Where the clients install the server from: a git URL or a local checkout (for testing)",
    )
    parser.add_argument("-y", "--yes", action="store_true", help="Set up every client found, no questions")
    options = parser.parse_args(argv)
    project = None if options.global_ else options.project.resolve()
    interactive = sys.stdin.isatty() and not options.yes

    where = "all your projects" if project is None else f"the project in [bold]{project}[/]"
    console.print(Panel.fit(f"[bold]Marvin MCP setup[/]\nAdds Marvin to your AI clients for {where}."))

    api_key = os.environ.get("MARVIN_API_KEY", "")
    if not api_key and interactive:
        api_key = prompt("Marvin API key: ", is_password=True).strip()
    if not api_key:
        raise SetupError("No API key given. Create one in the Marvin app under Account → API keys.")
    with console.status("Checking your API key…"):
        check_key(api_key, options.api_url)
    console.print("[green]✓[/] API key works")

    found = targets(project)
    missing = [name for cli, name in (("claude", "Claude Code"), ("codex", "Codex")) if not shutil.which(cli)]
    if missing:
        console.print(f"[dim]Not found on your PATH: {', '.join(missing)}.[/]")
    if not found:
        raise SetupError("Found neither Claude Code nor Codex. Install one, or run setup --global.")
    chosen = choose(found, interactive)
    if chosen is None:
        console.print("Cancelled; nothing changed.")
        return 130
    if not chosen:
        console.print("Nothing selected; nothing changed.")
        return 0

    source = options.source
    if Path(source).expanduser().is_dir():
        source = str(Path(source).expanduser().resolve())
    server = server_for(api_key, options.api_url, source)
    results = Table(show_header=False, box=None, padding=(0, 1))
    results.add_column(no_wrap=True)
    results.add_column(no_wrap=True)
    results.add_column(overflow="fold")
    notes: list[tuple[str, str]] = []
    for target in chosen:
        try:
            outcome = target.install(server)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode(errors="replace").strip() or str(exc)
            results.add_row("[red]✗[/]", target.label, f"[red]failed:[/] {detail}")
            continue
        except (SetupError, OSError, ValueError) as exc:
            results.add_row("[red]✗[/]", target.label, f"[red]not set up:[/] {exc}")
            continue
        if target.instructions:
            notes.append((target.label, outcome))
            results.add_row("[yellow]→[/]", target.label, "add it by hand, see below")
        else:
            results.add_row("[green]✓[/]", target.label, outcome)
    console.print(results)
    for label, text in notes:
        console.print(Panel(text, title=label, title_align="left"))
    if project is not None:
        console.print(
            "[dim]Claude Desktop and ChatGPT have no per-project setting: run setup --global for them.[/]"
        )
    console.print("Restart the clients you set up to load Marvin.")
    return 0


def main(argv: list[str]) -> int:
    console = Console()
    try:
        return run(argv, console)
    except SetupError as exc:
        console.print(f"[red]✗[/] {exc}")
        return 1
    except (KeyboardInterrupt, EOFError):
        console.print("\nCancelled; nothing changed.")
        return 130
