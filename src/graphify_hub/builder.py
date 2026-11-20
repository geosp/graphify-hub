"""Clone or update each project's repository and (re)build its Graphify graph.

Builds run on a small thread pool. A project is built by at most one thread at
a time, and the graph currently on disk keeps serving queries while a rebuild
runs.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from graphify_hub.config import HubSettings
from graphify_hub.registry import BUILDING, FAILED, READY, Registry

logger = logging.getLogger(__name__)

# Applied to every git command: no hooks, https only, no credential helpers, and
# symlinks checked out as plain files so a repo cannot point the hub at files
# outside its own checkout.
_GIT_CONFIG = (
    "core.hooksPath=/dev/null",
    "core.symlinks=false",
    "credential.helper=",
    "advice.detachedHead=false",
)
_ERROR_TAIL = 2000


class BuildError(RuntimeError):
    pass


class Builder:
    #: Git transports the hub may use. Tests widen this to include "file".
    allowed_protocols: tuple[str, ...] = ("https",)

    def __init__(self, settings: HubSettings, registry: Registry) -> None:
        self._settings = settings
        self._registry = registry
        self._pool = ThreadPoolExecutor(
            max_workers=settings.build_workers, thread_name_prefix="graphify-build"
        )
        self._guard = threading.Lock()
        self._inflight: set[str] = set()
        self._project_locks: dict[str, threading.Lock] = {}
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None

    # -- paths ------------------------------------------------------------

    def repo_dir(self, name: str) -> Path:
        return self._settings.projects_dir / name / "repo"

    def graph_dir(self, name: str) -> Path:
        return self._settings.projects_dir / name / "graph"

    def graph_path(self, name: str) -> Path:
        return self.graph_dir(name) / "graph.json"

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        for name in self._registry.reset_interrupted():
            logger.info("resuming build for %s", name)
            self.submit(name)
        if self._settings.refresh_interval > 0:
            self._scheduler = threading.Thread(
                target=self._schedule, name="graphify-refresh", daemon=True
            )
            self._scheduler.start()

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _schedule(self) -> None:
        while not self._stop.wait(self._settings.refresh_interval):
            for project in self._registry.all():
                if project.status in (READY, FAILED):
                    self.submit(project.name)

    # -- jobs -------------------------------------------------------------

    def is_building(self, name: str) -> bool:
        with self._guard:
            return name in self._inflight

    def submit(self, name: str, *, force: bool = False) -> bool:
        """Queue a build. Returns False if one is already queued or running."""
        with self._guard:
            if name in self._inflight:
                return False
            self._inflight.add(name)
            self._project_locks.setdefault(name, threading.Lock())
        self._registry.update(name, status=BUILDING)
        self._pool.submit(self._run, name, force)
        return True

    def build_now(self, name: str, *, force: bool = False) -> None:
        """Build synchronously (tests and the CLI)."""
        with self._guard:
            self._inflight.add(name)
            self._project_locks.setdefault(name, threading.Lock())
        self._run(name, force)

    def delete(self, name: str) -> None:
        """Delete a removed project's files once any running build has finished."""

        def _delete() -> None:
            with self._lock_for(name):
                if self._registry.get(name) is None:
                    shutil.rmtree(self._settings.projects_dir / name, ignore_errors=True)

        self._pool.submit(_delete)

    def _lock_for(self, name: str) -> threading.Lock:
        with self._guard:
            return self._project_locks.setdefault(name, threading.Lock())

    def _run(self, name: str, force: bool) -> None:
        try:
            with self._lock_for(name):
                self._build(name, force)
        except Exception as exc:  # noqa: BLE001 - recorded on the project
            logger.warning("build for %s failed: %s", name, exc)
            if self._registry.get(name) is not None:
                self._registry.update(name, status=FAILED, last_error=str(exc)[-_ERROR_TAIL:])
        finally:
            with self._guard:
                self._inflight.discard(name)

    def _build(self, name: str, force: bool) -> None:
        project = self._registry.get(name)
        if project is None:
            return  # removed while queued
        self._registry.update(name, status=BUILDING)
        repo, graph_dir = self.repo_dir(name), self.graph_dir(name)

        self._sync_repo(repo, project.repo_url, project.branch)
        head = self._git(["rev-parse", "HEAD"], cwd=repo).strip()
        size_mb = _tree_size(repo) / (1024 * 1024)
        if size_mb > self._settings.max_repo_mb:
            shutil.rmtree(repo, ignore_errors=True)
            raise BuildError(
                f"checkout is {size_mb:.0f} MB, over HUB_MAX_REPO_MB={self._settings.max_repo_mb}"
            )

        graph = graph_dir / "graph.json"
        if not force and head == project.built_sha and graph.exists():
            self._registry.update(name, status=READY, head_sha=head, last_error=None)
            return

        if project.built_sha is None or force:
            # First build for this registration: never reuse output a previous
            # project of the same name left behind.
            shutil.rmtree(graph_dir, ignore_errors=True)
        graph_dir.mkdir(parents=True, exist_ok=True)
        if graph.exists():
            args = ["update", str(repo)]
        else:
            args = ["extract", str(repo), "--code-only"]
        self._graphify(args, cwd=repo, out=graph_dir)
        if not graph.exists():
            raise BuildError("graphify finished without writing graph.json")

        nodes, edges = _graph_counts(graph)
        self._registry.update(
            name,
            status=READY,
            head_sha=head,
            built_sha=head,
            built_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
            last_error=None,
            node_count=nodes,
            edge_count=edges,
        )
        logger.info("built %s at %s: %d nodes, %d edges", name, head[:7], nodes, edges)

    # -- git and graphify -------------------------------------------------

    def _sync_repo(self, repo: Path, url: str, branch: str) -> None:
        if (repo / ".git").is_dir():
            current = self._git(["remote", "get-url", "origin"], cwd=repo).strip()
            if current != url:
                shutil.rmtree(repo)
        if not (repo / ".git").is_dir():
            shutil.rmtree(repo, ignore_errors=True)
            repo.parent.mkdir(parents=True, exist_ok=True)
            clone = ["clone", "--depth", "1", "--single-branch", "--no-tags", "--branch", branch]
            self._git([*clone, "--", url, str(repo)], cwd=repo.parent)
            return
        self._git(["fetch", "--depth", "1", "--no-tags", "origin", "--", branch], cwd=repo)
        self._git(["reset", "--hard", "FETCH_HEAD"], cwd=repo)
        self._git(["clean", "-ffdxq"], cwd=repo)

    def _base_env(self) -> dict[str, str]:
        home = self._settings.data_dir / "home"
        home.mkdir(parents=True, exist_ok=True)
        return {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(home),
            "LANG": "C.UTF-8",
        }

    def _git(self, args: list[str], *, cwd: Path) -> str:
        env = self._base_env() | {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ASKPASS": "/bin/false",
        }
        config = [*_GIT_CONFIG, "protocol.allow=never"]
        config += [f"protocol.{proto}.allow=always" for proto in self.allowed_protocols]
        cmd = ["git"]
        for item in config:
            cmd += ["-c", item]
        return _run(
            cmd + args,
            label=f"git {args[0]}",
            cwd=cwd,
            env=env,
            timeout=self._settings.build_timeout,
        )

    def _graphify(self, args: list[str], *, cwd: Path, out: Path) -> str:
        env = self._base_env() | {
            "GRAPHIFY_BACKEND": "none",
            "GRAPHIFY_OUT": str(out),
            "PYTHONHASHSEED": "0",
        }
        return _run(
            [sys.executable, "-m", "graphify", *args],
            label=f"graphify {args[0]}",
            cwd=cwd,
            env=env,
            timeout=self._settings.build_timeout,
        )


def _run(cmd: list[str], *, label: str, cwd: Path, env: dict[str, str], timeout: int) -> str:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f"{label} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-_ERROR_TAIL:]
        raise BuildError(f"{label} failed (exit {proc.returncode}): {tail}")
    return proc.stdout


def _tree_size(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def _graph_counts(path: Path) -> tuple[int, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return len(data.get("nodes", [])), len(data.get("links", data.get("edges", [])))
