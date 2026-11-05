# Marvin MCP

Ask Marvin, Marvin Labs' equity research analyst, from Claude Desktop, Claude Code or any other MCP client. It runs locally over stdio and only calls the Marvin Labs public API (`/v1/responses`, `/v1/conversations`) with your own API key. Conversations you start here show up in the Marvin app, and app conversations can be continued from here.

## Setup

You need:

1. **API access.** The Marvin API is in early access and has to be enabled for your account; until it is, every call returns 404.
2. **An API key.** In the app: Account → API keys → Add new key.
3. **uv** (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

### Claude Code

```bash
claude mcp add marvin \
  --env MARVIN_API_KEY=ak_... \
  -- uvx --refresh --from "git+https://github.com/Marvin-Labs/marvin-mcp" marvin-mcp
```

### Claude Desktop

In `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "marvin": {
      "command": "uvx",
      "args": [
        "--refresh",
        "--from",
        "git+https://github.com/Marvin-Labs/marvin-mcp",
        "marvin-mcp"
      ],
      "env": { "MARVIN_API_KEY": "ak_..." }
    }
  }
}
```

`--refresh` fetches the latest version each time your client starts the server. To stay on one version instead, drop it and pin a commit: `git+https://github.com/Marvin-Labs/marvin-mcp@<sha>`.

### Codex

```bash
codex mcp add marvin --env MARVIN_API_KEY=ak_... \
  -- uvx --refresh --from "git+https://github.com/Marvin-Labs/marvin-mcp" marvin-mcp
```

### Other clients

Any MCP client that starts local (stdio) servers works: the command is `uvx`, the arguments are `--refresh --from git+https://github.com/Marvin-Labs/marvin-mcp marvin-mcp`, and `MARVIN_API_KEY` goes in the server's environment.

## Tools

| Tool | Does |
|---|---|
| `ask_marvin(question, conversation_id?)` | Asks a question, in a new conversation or continuing one. Returns the answer, or a `response_id` if it is still working |
| `get_response(response_id)` | Waits a little longer for an answer that is still running |
| `cancel_response(response_id)` | Stops a running answer |
| `list_conversations(limit?, cursor?)` | Your conversations, most recent first, including the app's |
| `get_conversation(conversation_id, limit?, cursor?)` | A conversation's questions and answers |

Answers often take a few minutes. MCP clients give up on a tool call long before that, so `ask_marvin` starts the answer in the background and waits for at most `MARVIN_WAIT_SECONDS` (default 50). If the answer isn't ready by then, the model calls `get_response` until it is.

## Configuration

| Variable | Default | |
|---|---|---|
| `MARVIN_API_KEY` | (required) | Your Marvin API key |
| `MARVIN_API_URL` | `https://app-api.marvin-labs.com` | Another environment, e.g. stg or a local API (`http://localhost:<port>`) |
| `MARVIN_WAIT_SECONDS` | `50` | How long one tool call waits for an answer |

Every answer is a normal Marvin chat turn and uses your credits the same way.

## Development

```bash
uv run pytest
MARVIN_API_KEY=ak_... MARVIN_API_URL=http://localhost:<port> uv run marvin-mcp   # stdio
```

This repository is published automatically from Marvin Labs' main codebase, where the client is maintained, and is replaced on every release. Changes made here are overwritten, so it does not take pull requests.

## Licence

MIT, see [LICENSE](LICENSE).
