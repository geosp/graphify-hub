# graphify-hub

graphify-hub is one MCP server that indexes many Git repositories with
[Graphify](https://github.com/geosp/graphify) and answers code-graph questions about each of
them. Examples are "who calls this?", "what does this module depend on?" and "how does A reach
B?".

An agent tells the hub which repository it is working in. The hub clones the repository, builds
a code graph (tree-sitter only, with no LLM, so the code never leaves the machine), refreshes it
as new commits land, and keeps everything across restarts.

It is built on [mcp-service-kit](https://github.com/geosp/mcp-service-kit) (FastMCP) and calls
Graphify in-process through `graphify.query_api`.

## Run it

You need Docker with Compose. Run these commands on any host:

```bash
git clone https://github.com/geosp/graphify-hub && cd graphify-hub
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # put this in HUB_TOKEN
docker compose up -d --build
```

The server listens on `http://<host>:${HUB_PORT:-3000}/mcp`. Registered projects and their graphs
are stored in the `graphify-hub-data` volume. To move the hub to another host, clone the repo
there, copy `.env` and run the same commands. The registry rebuilds missing graphs on first start.
To take existing graphs along, copy the volume as well.

## Connect a client

With Claude Code, register the hub once at user scope, so every project can use it:

```bash
claude mcp add --scope user --transport http graphify-hub http://<host>:3000/mcp \
  --header "Authorization: Bearer $HUB_TOKEN"
```

Any MCP client that supports streamable HTTP and a bearer header works the same way.

## Use it

The server sends these steps to clients as instructions:

1. `list_projects`. If your repository is missing, call
   `register_project(name="praxismesh", repo_url="https://github.com/geosp/praxismesh")`.
2. Call `project_status(name)` until `status` is `ready`. A mid-sized repo (about 35k lines)
   builds in about 5 s.
3. Run queries with `project=<name>`: `query_graph`, `get_node`, `get_neighbors`,
   `shortest_path`, `get_community`, `get_section`, `god_nodes`, `graph_stats`.
4. After pushing, call `refresh_project(name)`. It is also refreshed every
   `HUB_REFRESH_INTERVAL` seconds. Unchanged commits are skipped.

Each answer starts with `[project <name> @ <commit>, built <time>]`, so you can see which commit
it reflects. Uncommitted local changes are never in the graph.

| Tool | Purpose |
|---|---|
| `list_projects` | All projects with status, indexed commit, counts and last error |
| `register_project(name, repo_url, branch="main")` | Add a public repository and build it in the background |
| `project_status(name)` | One project's state |
| `refresh_project(name, force=False)` | Fetch the branch and rebuild if the commit changed |
| `remove_project(name)` | Unregister a project and delete its checkout and graph |
| `query_graph(project, question, …)` | BFS/DFS search returning the relevant subgraph as text |
| `get_node` / `get_neighbors` | One symbol's details and its edges |
| `shortest_path(project, source, target)` | How two concepts connect |
| `get_community` / `god_nodes` / `graph_stats` | Structure overview |
| `get_section` | A Markdown section from the checkout |

## Configuration

All settings are environment variables. See [.env.example](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `HUB_TOKEN` | none | Bearer token. Without it the HTTP server refuses to start, unless you pass `--no-auth` |
| `HUB_ALLOWED_REPOS` | `https://github.com/` | Comma-separated https prefixes that may be registered |
| `HUB_REFRESH_INTERVAL` | `900` | Seconds between automatic refreshes (`0` = off) |
| `HUB_BUILD_TIMEOUT` | `900` | Seconds before a clone, fetch or build is killed |
| `HUB_MAX_REPO_MB` | `500` | Largest checkout kept |
| `HUB_MAX_CONTEXTS` | `8` | Graphs kept loaded in memory |
| `HUB_BUILD_WORKERS` | `2` | Builds that run at once |
| `HUB_DATA_DIR` | `/data` | Registry, clones and graphs |
| `HUB_BIND` / `HUB_PORT` | `0.0.0.0` / `3000` | Published address (compose only) |

## Security model

- **Public repositories only.** The hub holds no Git credentials. Clones use anonymous HTTPS with
  prompts disabled, so a private repository fails fast.
- **Repository URLs are checked before git sees them.** A URL must be `https://host/owner/repo`
  with no credentials, port, query or `..` segments, and it must match `HUB_ALLOWED_REPOS`.
  `git@host:owner/repo` is rewritten to https. Project and branch names follow strict patterns.
  Filesystem paths come only from the registry, never from client input.
- **Nothing from a repository runs.** Git hooks are disabled. Only the https protocol is allowed,
  with no `file://`, `ext::` or submodules. Symlinks are checked out as plain files, so a repo
  cannot point the hub at files outside its checkout.
- **No LLM calls.** Builds run with `GRAPHIFY_BACKEND=none` and `--code-only`. The only outbound
  traffic is to the Git host.
- **Container.** It runs as a non-root user with a read-only root filesystem, all capabilities
  dropped and `no-new-privileges`. Only the data volume and `/tmp` are writable.
- **The bearer token is shared.** Every client with the token can register and remove projects.
  Keep the hub on a trusted network.

## Develop

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
HUB_DATA_DIR=./data uv run graphify-hub --mode http --no-auth   # local, no token
```

To use a local checkout of the Graphify fork instead of the pinned commit, add this to
`[tool.uv.sources]`:

```toml
graphifyy = { path = "../graphify", editable = true }
```
