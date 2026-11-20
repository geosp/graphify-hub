"""The MCP server and the ``graphify-hub`` command."""

from __future__ import annotations

import hmac
from importlib.metadata import version

from fastmcp.server.auth import AccessToken, AuthProvider, TokenVerifier
from mcp_service_kit import BaseMCPServer, create_main_with_modes

from graphify_hub.config import HubConfig, load_config
from graphify_hub.service import HubService

INSTRUCTIONS = """\
graphify-hub indexes Git repositories as code knowledge graphs (functions, classes,
modules, and the calls/imports between them) and answers structural questions.

1. Call list_projects. If the repository you are working in is missing, call
   register_project(name, repo_url) with its public https URL, then poll
   project_status until status is "ready" (usually seconds).
2. Pass project=<name> to every query tool: query_graph for open questions,
   get_node / get_neighbors for one symbol, shortest_path to connect two.
3. Answers reflect the commit shown in the first line. After pushing changes,
   call refresh_project. Uncommitted local edits are never in the graph.
"""


class StaticBearer(TokenVerifier):
    """Accepts exactly one shared bearer token (compared in constant time)."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token.encode("utf-8")

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode("utf-8"), self._token):
            return AccessToken(token=token, client_id="graphify-hub-client", scopes=[])
        return None


class HubServer(BaseMCPServer):
    service_title = "graphify-hub"
    service_description = "Multi-project Graphify code-graph MCP server"
    service_version = version("graphify-hub")
    instructions = INSTRUCTIONS

    def create_auth_provider(self) -> AuthProvider | None:
        service = self.service
        token = service.settings.token if isinstance(service, HubService) else None
        if token is None or not token.get_secret_value():
            return None  # the kit then refuses to serve HTTP unless --no-auth
        return StaticBearer(token.get_secret_value())


def _service(config: HubConfig) -> HubService:
    return HubService(config)


main = create_main_with_modes(
    HubServer,
    service_factory=_service,
    config_factory=load_config,
    service_name="graphify-hub",
    description="Multi-project Graphify code-graph MCP server",
)

if __name__ == "__main__":
    main()
