from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from mcp_service_kit import BaseServerConfig

from graphify_hub.builder import Builder
from graphify_hub.config import HubConfig, HubSettings
from graphify_hub.server import HubServer
from graphify_hub.service import HubService

SOURCES = {
    "app/__init__.py": "",
    "app/main.py": ("from app.parser import parse\n\n\ndef main(text):\n    return parse(text)\n"),
    "app/parser.py": (
        "from app.lexer import tokenize\n\n\n"
        "def parse(text):\n    return [t.upper() for t in tokenize(text)]\n"
    ),
    "app/lexer.py": "def tokenize(text):\n    return text.split()\n",
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """A local git repo with a tiny Python package on branch main."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for rel, text in SOURCES.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.fixture(autouse=True)
def allow_file_remotes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Builder, "allowed_protocols", ("https", "file"))


@pytest.fixture
def settings(tmp_path: Path) -> HubSettings:
    return HubSettings(data_dir=tmp_path / "data", refresh_interval=0, token="s3cret")


@pytest.fixture
def config(settings: HubSettings) -> HubConfig:
    return HubConfig(server=BaseServerConfig(transport="stdio"), hub=settings)


@pytest.fixture
def service(config: HubConfig):
    svc = HubService(config)
    svc.initialize()
    yield svc
    svc.cleanup()


@pytest.fixture
def mcp(config: HubConfig, service: HubService):
    return HubServer(config.server, service).create_mcp_app()
