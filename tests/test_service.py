from __future__ import annotations

import time

import anyio
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from mcp_service_kit import BaseServerConfig

from graphify_hub import validation
from graphify_hub.registry import READY
from graphify_hub.server import HubServer, StaticBearer
from graphify_hub.service import HubService


def _text(result) -> str:
    return result.content[0].text


def _ready(service: HubService, name: str, upstream) -> None:
    service.registry.add(name, upstream.as_uri(), "main")
    service.builder.build_now(name)
    assert service.registry.get(name).status == READY, service.registry.get(name).last_error


async def test_tools_listed(mcp):
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert {
        "list_projects",
        "register_project",
        "project_status",
        "refresh_project",
        "remove_project",
        "query_graph",
        "get_node",
        "get_neighbors",
        "shortest_path",
        "get_community",
        "get_section",
        "god_nodes",
        "graph_stats",
    } <= names
    assert not names & {"list_prs", "get_pr_impact", "triage_prs"}


async def test_query_tools_answer_per_project(mcp, service, upstream):
    _ready(service, "demo", upstream)
    async with Client(mcp) as client:
        stats = _text(await client.call_tool("graph_stats", {"project": "demo"}))
        assert stats.startswith("[project demo @ ")
        assert "Nodes:" in stats
        node = _text(await client.call_tool("get_node", {"project": "demo", "label": "parse"}))
        assert "parser.py" in node
        neighbors = _text(
            await client.call_tool("get_neighbors", {"project": "demo", "label": "parse"})
        )
        assert "tokenize" in neighbors
        path = _text(
            await client.call_tool(
                "shortest_path", {"project": "demo", "source": "main", "target": "tokenize"}
            )
        )
        assert "parse" in path
        q = _text(await client.call_tool("query_graph", {"project": "demo", "question": "parse"}))
        assert "parse" in q


async def test_unknown_project_is_a_tool_error(mcp):
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="unknown project 'ghost'"):
            await client.call_tool("graph_stats", {"project": "ghost"})


@pytest.mark.parametrize(
    ("args", "match"),
    [
        ({"name": "Bad_Name", "repo_url": "https://github.com/a/b"}, "project name"),
        ({"name": "x", "repo_url": "file:///etc"}, "https"),
        (
            {"name": "x", "repo_url": "https://github.com/a/b", "branch": "--upload-pack=x"},
            "branch",
        ),
    ],
)
async def test_register_rejects_bad_input(mcp, args, match):
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match=match):
            await client.call_tool("register_project", args)


async def test_register_builds_in_background(mcp, service, upstream, monkeypatch):
    # Real registrations need https; point the canonical URL at the local fixture.
    monkeypatch.setattr(validation, "repo_url", lambda url, allowed: upstream.as_uri())
    async with Client(mcp) as client:
        first = (
            await client.call_tool(
                "register_project", {"name": "demo", "repo_url": "https://github.com/o/demo"}
            )
        ).structured_content
        assert first["name"] == "demo"
        deadline = time.monotonic() + 60
        status = first
        while status["status"] != READY and time.monotonic() < deadline:
            await anyio.sleep(0.2)
            status = (await client.call_tool("project_status", {"name": "demo"})).structured_content
        assert status["status"] == READY, status
        again = (
            await client.call_tool(
                "register_project", {"name": "demo", "repo_url": "https://github.com/o/demo"}
            )
        ).structured_content
        assert again["built_sha"] == status["built_sha"]


async def test_name_clash_with_other_repo(mcp, service, upstream):
    service.registry.add("demo", "https://github.com/o/one.git", "main")
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="already registered"):
            await client.call_tool(
                "register_project", {"name": "demo", "repo_url": "https://github.com/o/two"}
            )


async def test_projects_do_not_mix(mcp, service, upstream, tmp_path):
    _ready(service, "one", upstream)
    from conftest import git

    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    (other / "zebra.py").write_text("def stripes():\n    return 1\n")
    git(other, "add", ".")
    git(other, "commit", "-q", "-m", "z")
    _ready(service, "two", other)
    async with Client(mcp) as client:
        missing = _text(await client.call_tool("get_node", {"project": "two", "label": "parse"}))
        found = _text(await client.call_tool("get_node", {"project": "two", "label": "stripes"}))
    assert "No node matching 'parse'" in missing
    assert "zebra.py" in found


async def test_remove_project(mcp, service, upstream):
    _ready(service, "demo", upstream)
    root = service.settings.projects_dir / "demo"
    async with Client(mcp) as client:
        await client.call_tool("remove_project", {"name": "demo"})
        assert (await client.call_tool("list_projects", {})).structured_content["result"] == []
    deadline = time.monotonic() + 10
    while root.exists() and time.monotonic() < deadline:
        await anyio.sleep(0.1)
    assert not root.exists()


async def test_static_bearer():
    verifier = StaticBearer("s3cret")
    assert await verifier.verify_token("s3cret") is not None
    assert await verifier.verify_token("s3cre") is None
    assert await verifier.verify_token("") is None


def test_http_without_token_refuses_to_start(config):
    config.hub.token = None
    svc = HubService(config)
    svc.initialize()
    try:
        server = HubServer(BaseServerConfig(transport="http"), svc)
        with pytest.raises(ValueError):
            server.create_mcp_app()
    finally:
        svc.cleanup()
