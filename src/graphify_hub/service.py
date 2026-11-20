"""MCP tools: manage registered projects and query their graphs."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

import anyio
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from graphify import query_api
from mcp_service_kit import BaseService
from pydantic import Field

from graphify_hub import validation
from graphify_hub.builder import Builder
from graphify_hub.config import HubConfig
from graphify_hub.registry import BUILDING, Project, Registry

logger = logging.getLogger(__name__)

ProjectArg = Annotated[str, Field(description="Registered project name (see list_projects).")]
TokenBudget = Annotated[int, Field(ge=100, le=50000, description="Max output tokens.")]


class HubService(BaseService):
    def __init__(self, config: HubConfig) -> None:
        self.settings = config.hub
        self.registry: Registry | None = None
        self.builder: Builder | None = None
        self._cache: query_api.GraphCache | None = None

    # -- BaseService ------------------------------------------------------

    def get_service_name(self) -> str:
        return "graphify-hub"

    def initialize(self) -> None:
        self.settings.projects_dir.mkdir(parents=True, exist_ok=True)
        self.registry = Registry(self.settings.registry_path)
        self.builder = Builder(self.settings, self.registry)
        self._cache = query_api.GraphCache(self.settings.max_contexts)
        self.builder.start()
        logger.info(
            "graphify-hub ready: %d project(s), data in %s",
            len(self.registry.all()),
            self.settings.data_dir,
        )

    def cleanup(self) -> None:
        if self.builder:
            self.builder.stop()
        if self.registry:
            self.registry.close()

    # -- helpers ----------------------------------------------------------

    def _reg(self) -> Registry:
        if self.registry is None:
            raise ToolError("graphify-hub is not initialised")
        return self.registry

    def _bld(self) -> Builder:
        if self.builder is None:
            raise ToolError("graphify-hub is not initialised")
        return self.builder

    def _project(self, name: str) -> Project:
        project = self._reg().get(name)
        if project is None:
            known = ", ".join(p.name for p in self._reg().all()) or "none"
            raise ToolError(
                f"unknown project {name!r} (registered: {known}). "
                "Call register_project(name, repo_url) first."
            )
        return project

    def _describe(self, project: Project) -> dict[str, Any]:
        info = project.as_dict()
        info["building"] = self._bld().is_building(project.name)
        return info

    async def _query(self, name: str, tool: str, arguments: dict[str, Any]) -> str:
        project = self._project(name)
        graph = self._bld().graph_path(project.name)
        if not graph.exists():
            raise ToolError(
                f"project {name!r} has no graph yet (status: {project.status}"
                + (f", error: {project.last_error}" if project.last_error else "")
                + "). Check project_status."
            )
        cache = self._cache
        if cache is None:
            raise ToolError("graphify-hub is not initialised")

        def run() -> str:
            ctx = query_api.load(cache, graph)
            return query_api.run_tool(tool, ctx, arguments)

        try:
            text = await anyio.to_thread.run_sync(run)
        except query_api.ToolError as exc:
            raise ToolError(str(exc)) from exc
        sha = (project.built_sha or "")[:7]
        note = "; rebuild in progress" if project.status == BUILDING else ""
        return f"[project {project.name} @ {sha}, built {project.built_at}{note}]\n{text}"

    # -- tools ------------------------------------------------------------

    def register_mcp_tools(self, mcp: FastMCP) -> None:  # noqa: C901 - one tool per block
        @mcp.tool()
        async def list_projects() -> list[dict[str, Any]]:
            """List every project the hub indexes, with build status and commit."""
            return [self._describe(p) for p in self._reg().all()]

        @mcp.tool()
        async def register_project(
            name: Annotated[
                str, Field(description="Short project name, e.g. 'praxismesh' (a-z, 0-9, '-').")
            ],
            repo_url: Annotated[
                str, Field(description="Public repo URL, e.g. https://github.com/owner/repo")
            ],
            branch: Annotated[str, Field(description="Branch to index.")] = "main",
        ) -> dict[str, Any]:
            """Register a public Git repository and start building its code graph.

            Returns immediately; the build runs in the background (seconds for
            a mid-sized repo). Poll project_status until status is 'ready'.
            Registering an existing name with the same repo and branch is a no-op.
            """
            try:
                name_ = validation.project_name(name)
                url = validation.repo_url(repo_url, self.settings.allowed_prefixes)
                branch_ = validation.branch_name(branch)
            except validation.ValidationError as exc:
                raise ToolError(str(exc)) from exc
            existing = self._reg().get(name_)
            if existing is not None:
                if (existing.repo_url, existing.branch) != (url, branch_):
                    raise ToolError(
                        f"project {name_!r} is already registered for {existing.repo_url} "
                        f"({existing.branch}); remove it first or pick another name"
                    )
                return self._describe(existing)
            self._reg().add(name_, url, branch_)
            self._bld().submit(name_)
            return self._describe(self._project(name_))

        @mcp.tool()
        async def project_status(name: ProjectArg) -> dict[str, Any]:
            """Show one project's status: commit indexed, build time, counts, last error."""
            return self._describe(self._project(name))

        @mcp.tool()
        async def refresh_project(
            name: ProjectArg,
            force: Annotated[
                bool, Field(description="Rebuild from scratch even if the commit is unchanged.")
            ] = False,
        ) -> dict[str, Any]:
            """Fetch the latest commit of the project's branch and rebuild if it changed."""
            project = self._project(name)
            self._bld().submit(project.name, force=force)
            return self._describe(self._project(name))

        @mcp.tool()
        async def remove_project(name: ProjectArg) -> str:
            """Unregister a project and delete its checkout and graph."""
            project = self._project(name)
            self._reg().remove(project.name)
            self._bld().delete(project.name)
            return f"removed {project.name}"

        @mcp.tool()
        async def query_graph(
            project: ProjectArg,
            question: Annotated[str, Field(description="Natural language question or keywords.")],
            mode: Annotated[
                Literal["bfs", "dfs"],
                Field(description="bfs = broad context, dfs = trace a specific path."),
            ] = "bfs",
            depth: Annotated[int, Field(ge=1, le=6, description="Traversal depth.")] = 3,
            token_budget: TokenBudget = 2000,
            context_filter: Annotated[
                list[str] | None,
                Field(description="Edge-context filter, e.g. ['call', 'field']."),
            ] = None,
            demote_docs: Annotated[
                bool, Field(description="Rank source code above docs/config.")
            ] = True,
        ) -> str:
            """Search a project's code graph (BFS or DFS); returns relevant nodes and edges."""
            args: dict[str, Any] = {
                "question": question,
                "mode": mode,
                "depth": depth,
                "token_budget": token_budget,
                "demote_docs": demote_docs,
            }
            if context_filter:
                args["context_filter"] = context_filter
            return await self._query(project, "query_graph", args)

        @mcp.tool()
        async def get_node(
            project: ProjectArg,
            label: Annotated[str, Field(description="Node label or ID.")],
        ) -> str:
            """Full details for one node: source file and line, type, community, degree."""
            return await self._query(project, "get_node", {"label": label})

        @mcp.tool()
        async def get_neighbors(
            project: ProjectArg,
            label: Annotated[str, Field(description="Node label or ID.")],
            relation_filter: Annotated[
                str, Field(description="Only edges of this relation, e.g. 'calls'.")
            ] = "",
            token_budget: TokenBudget = 2000,
        ) -> str:
            """Direct neighbours of a node (callers, callees, imports...) with edge details."""
            return await self._query(
                project,
                "get_neighbors",
                {"label": label, "relation_filter": relation_filter, "token_budget": token_budget},
            )

        @mcp.tool()
        async def shortest_path(
            project: ProjectArg,
            source: Annotated[str, Field(description="Source concept label or keyword.")],
            target: Annotated[str, Field(description="Target concept label or keyword.")],
            max_hops: Annotated[int, Field(ge=1, le=20)] = 8,
            undirected: Annotated[bool, Field(description="Ignore stored edge direction.")] = False,
        ) -> str:
            """Shortest path between two concepts, with the confidence of its weakest hop."""
            return await self._query(
                project,
                "shortest_path",
                {
                    "source": source,
                    "target": target,
                    "max_hops": max_hops,
                    "undirected": undirected,
                },
            )

        @mcp.tool()
        async def get_community(
            project: ProjectArg,
            community_id: Annotated[int, Field(ge=0, description="Community ID (0 = largest).")],
            token_budget: TokenBudget = 2000,
        ) -> str:
            """All nodes in one community (a cluster of closely related code)."""
            return await self._query(
                project,
                "get_community",
                {"community_id": community_id, "token_budget": token_budget},
            )

        @mcp.tool()
        async def get_section(
            project: ProjectArg,
            label: Annotated[
                str, Field(description="Heading label, node ID or 'file.md::Heading'.")
            ],
            max_chars: Annotated[int, Field(ge=1, le=50000)] = 8000,
        ) -> str:
            """Text of a Markdown section from the project's checkout."""
            return await self._query(
                project, "get_section", {"label": label, "max_chars": max_chars}
            )

        @mcp.tool()
        async def god_nodes(
            project: ProjectArg,
            top_n: Annotated[int, Field(ge=1, le=100)] = 10,
        ) -> str:
            """The most connected nodes: the project's core abstractions."""
            return await self._query(project, "god_nodes", {"top_n": top_n})

        @mcp.tool()
        async def graph_stats(project: ProjectArg) -> str:
            """Node, edge and community counts and the edge-confidence breakdown."""
            return await self._query(project, "graph_stats", {})
