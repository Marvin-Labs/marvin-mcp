# Marvin MCP

Ask Marvin, Marvin Labs' equity research analyst, from Claude, ChatGPT, Claude Code or any other MCP client. Marvin Labs [hosts it for you](#claude-and-chatgpt) at `https://app-api.marvin-labs.com/v1/mcp`, or you can [run it locally](#claude-code-codex-and-desktop-apps) over stdio. Either way it only calls the Marvin Labs public API (`/v1/responses`, `/v1/conversations`, `/v1/agent_results`, `/v1/tools`) as you. Conversations you start here show up in the Marvin app, and app conversations can be continued from here.

## Setup

This is a private alpha. Please email marvin@marvin-labs.com for access.

### Claude and ChatGPT

[![Add to Claude](https://img.shields.io/badge/Claude-Add_connector-D97757?style=flat-square&logo=claude&logoColor=white)](https://claude.ai/customize/connectors)
[![Add to ChatGPT](https://img.shields.io/badge/ChatGPT-Add_MCP_server-10A37F?style=flat-square)](https://chatgpt.com/plugins)

There is nothing to install and no API key: you add the URL below once, then sign in with your Marvin account. The buttons open the right page, but neither app can fill in the form from a link yet, so paste the URL yourself:

```text
https://app-api.marvin-labs.com/v1/mcp
```

**Claude** (claude.ai, Desktop and mobile):

1. Open [Customize → Connectors](https://claude.ai/customize/connectors) on claude.ai or in Claude Desktop.
2. Click **+ Add**, then **Add custom connector**.
3. Name it `Marvin` and paste the URL.
4. Keep the detected sign-in settings (OAuth, with Claude's own OAuth client) and click **Add**.
5. Click **Connect** and sign in to Marvin.

To use it, click **+** in a chat, open **Connectors** and turn Marvin on. Once added, the connector also works in the mobile apps. On the Free plan it counts as your one custom connector. On Team and Enterprise plans an owner adds it once under [Organization settings → Connectors](https://claude.ai/admin-settings/connectors) (**Add → Custom → Web**), and then each member clicks **Connect** in their own Customize → Connectors.

**ChatGPT** (web, Business, Enterprise and Edu plans; your workspace admin may need to allow custom MCP servers):

1. Open [Plugins](https://chatgpt.com/plugins).
2. Click **+**, then **Add custom MCP server**.
3. Name it `Marvin`, add a short description, and paste the URL under **Connection**.
4. Choose **OAuth** for authentication, confirm the warning, then **Create as a plugin**.
5. Sign in to Marvin when ChatGPT asks.

To use it, type `@Marvin` in a new conversation.

**Claude Code:**

```bash
claude mcp add --transport http marvin https://app-api.marvin-labs.com/v1/mcp
```

Then run `/mcp` in Claude Code to sign in. To use an API key instead of signing in, add `--header "Authorization: Bearer ak_..."`.

Long research tool calls can time out on the hosted server. If you need them, run the server locally.

### Claude Code, Codex and desktop apps

To run the server on your own machine you need:

1. **An API key.** In the app: Account → API keys → Add new key.
2. **uv** (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

Then, from the project you want Marvin in, run:

```bash
uvx --from git+https://github.com/Marvin-Labs/marvin-mcp marvin-mcp setup
```

It asks for your API key, checks that it works, and adds Marvin to the clients it finds. By default that is **this project only**:

| Client | `setup` (this project) | `setup --global` (all projects) |
|---|---|---|
| Claude Code | yes, private to you (not in `.mcp.json`) | yes |
| Codex, CLI and app | yes, in `.codex/config.toml`, kept out of git; Codex loads it once you trust the project | yes |
| Claude Desktop | no per-project setting | yes |
| ChatGPT desktop | no per-project setting | prints the values to paste into its settings |

Run it again to change the key. `--yes` sets up every client found without asking; `MARVIN_API_KEY` in the environment skips the key prompt; `--api-url` points the clients at another environment, such as stg.

#### By hand

Any MCP client that starts local (stdio) servers works. The command is `uvx`, the arguments are `--refresh --from git+https://github.com/Marvin-Labs/marvin-mcp marvin-mcp`, and `MARVIN_API_KEY` goes in the server's environment. For example:

```bash
claude mcp add marvin --env MARVIN_API_KEY=ak_... \
  -- uvx --refresh --from "git+https://github.com/Marvin-Labs/marvin-mcp" marvin-mcp
codex mcp add marvin --env MARVIN_API_KEY=ak_... \
  -- uvx --refresh --from "git+https://github.com/Marvin-Labs/marvin-mcp" marvin-mcp
```

Claude Desktop on macOS does not see your shell's PATH, so give it the full path to `uvx` (`which uvx`).

`--refresh` fetches the latest version each time your client starts the server. To stay on one version instead, drop it and pin a commit: `git+https://github.com/Marvin-Labs/marvin-mcp@<sha>`.

## Tools

| Tool | Does |
|---|---|
| `ask_marvin(question, conversation_id?)` | Asks a question, in a new conversation or continuing one. Returns the answer, or a `response_id` if it is still working |
| `get_response(response_id)` | Waits a little longer for an answer that is still running |
| `cancel_response(response_id)` | Stops a running answer |
| `list_conversations(limit?, cursor?)` | Your conversations, most recent first, including the app's |
| `get_conversation(conversation_id, limit?, cursor?)` | A conversation's questions and answers |
| `list_agent_results(company?, agent?, mine?, limit?, cursor?)` | Reports Marvin's research agents have written that you can read: yours, shared with you, and public ones |
| `get_agent_result(agent_result_id)` | One report, in full |
| `get_format_guide()` | How to show the table and chart blocks in answers |

To research a company yourself instead of asking Marvin, use the same tools Marvin's analysts use. Each answers in one call, outside any conversation:

| Tool | Does |
|---|---|
| `list_documents(company, document_type?, period?, date_from?, date_to?, limit?, cursor?)` | A company's documents, newest first: filings, transcripts, press releases, Marvin's reports and your uploads |
| `search_documents(company, query, terms?, n_results?, document_type?, period?, date_from?, date_to?)` | The passages in a company's documents that answer a question, best first |
| `read_document(document_id, section?, query?, start_token_offset?, max_token_length?)` | A document by its id: whole, a window, or the text around a match |
| `explore_document(company, document_id, question, section?)` | What one document says about a question, as verbatim passages and a summary |
| `query_financials(company, queries, bias?)` | Reported figures and consensus estimates by metric and period |
| `get_financial_statements(company, statement?, periods?)` | A statement whole, every line in order; without `statement`, the list of statements |
| `get_financial_provenance(company, datapoints)` | The document and line each figure was read from, or a computed figure's inputs |

Document ids such as `fd:k7qm2xp` stay valid: keep them and pass them back to `read_document`. The financials tools cite figures with the same ids. `search_documents`, `explore_document` and `query_financials` use credits.

Answers often take a few minutes. MCP clients give up on a tool call long before that, so `ask_marvin` starts the answer in the background and waits for at most `MARVIN_WAIT_SECONDS` (default 50). If the answer isn't ready by then, the model calls `get_response` until it is.

Answers can contain tables and charts, written as JSON in ```` ```financial-table ```` and ```` ```echarts ```` blocks that the Marvin app renders. `get_format_guide` explains how to turn the blocks into markdown tables, or into charts if the client can draw them.

## Configuration

| Variable | Default | |
|---|---|---|
| `MARVIN_API_KEY` | (required) | Your Marvin API key |
| `MARVIN_API_URL` | `https://app-api.marvin-labs.com` | Another environment, e.g. stg or a local API (`http://localhost:<port>`) |
| `MARVIN_WAIT_SECONDS` | `50` | How long one tool call waits for an answer |

Every answer is a normal Marvin chat turn and uses your credits the same way.

Each request to the Marvin API sends `User-Agent: marvin-mcp/<version>` and the name of the tool that made it (`X-Marvin-Client-Tool`), so Marvin can see which tools are used. The server collects nothing else and sends no telemetry of its own.

## Development

```bash
uv run pytest
MARVIN_API_KEY=ak_... MARVIN_API_URL=http://localhost:<port> uv run marvin-mcp   # stdio
```

To try a change in real clients before it is published, run setup from your checkout and pass `--source`, so the clients also start the server from it (add `--api-url` for stg or a local API):

```bash
uvx --from <checkout> marvin-mcp setup --source <checkout>
```

uv rebuilds the checkout whenever a file under `src/` changes (`[tool.uv] cache-keys` in `pyproject.toml`), so the next run and the next client restart pick up your edits. Run setup again without `--source` to switch back to the published version.

This repository is published automatically from Marvin Labs' main codebase, where the client is maintained, and is replaced on every release. Changes made here are overwritten, so it does not take pull requests.

## Licence

MIT, see [LICENSE](LICENSE).
