from __future__ import annotations

import os
from pathlib import Path

from conftest import git

from graphify_hub.builder import Builder
from graphify_hub.registry import FAILED, PENDING, READY, Registry


def _make(settings) -> tuple[Registry, Builder]:
    registry = Registry(settings.registry_path)
    return registry, Builder(settings, registry)


def test_first_build(settings, upstream):
    registry, builder = _make(settings)
    registry.add("demo", upstream.as_uri(), "main")
    builder.build_now("demo")

    p = registry.get("demo")
    assert p.status == READY, p.last_error
    assert p.built_sha == git(upstream, "rev-parse", "HEAD")
    assert p.node_count and p.edge_count
    assert builder.graph_path("demo").is_file()
    # Graphify writes only to the graph directory, never into the checkout.
    assert git(builder.repo_dir("demo"), "status", "--porcelain") == ""


def test_refresh_without_new_commit_skips_rebuild(settings, upstream):
    registry, builder = _make(settings)
    registry.add("demo", upstream.as_uri(), "main")
    builder.build_now("demo")
    before = registry.get("demo")
    mtime = builder.graph_path("demo").stat().st_mtime_ns

    builder.build_now("demo")
    after = registry.get("demo")
    assert after.status == READY
    assert after.built_at == before.built_at
    assert builder.graph_path("demo").stat().st_mtime_ns == mtime


def test_refresh_picks_up_new_commit(settings, upstream):
    registry, builder = _make(settings)
    registry.add("demo", upstream.as_uri(), "main")
    builder.build_now("demo")
    first = registry.get("demo")

    (upstream / "app" / "emit.py").write_text(
        "from app.main import main\n\n\ndef emit(text):\n    print(main(text))\n"
    )
    git(upstream, "add", ".")
    git(upstream, "commit", "-q", "-m", "add emit")
    builder.build_now("demo")

    second = registry.get("demo")
    assert second.status == READY, second.last_error
    assert second.built_sha == git(upstream, "rev-parse", "HEAD") != first.built_sha
    assert second.node_count > first.node_count


def test_symlinks_are_checked_out_as_files(settings, upstream):
    os.symlink("/etc/passwd", upstream / "leak.py")
    git(upstream, "add", ".")
    git(upstream, "commit", "-q", "-m", "symlink")
    registry, builder = _make(settings)
    registry.add("demo", upstream.as_uri(), "main")
    builder.build_now("demo")

    leak = builder.repo_dir("demo") / "leak.py"
    assert leak.exists() and not leak.is_symlink()
    assert leak.read_text() == "/etc/passwd"


def test_missing_branch_fails_with_error(settings, upstream):
    registry, builder = _make(settings)
    registry.add("demo", upstream.as_uri(), "nope")
    builder.build_now("demo")
    p = registry.get("demo")
    assert p.status == FAILED
    assert "git clone failed" in p.last_error


def test_oversized_checkout_is_rejected(settings, upstream):
    (upstream / "blob.bin").write_bytes(os.urandom(2 * 1024 * 1024))
    git(upstream, "add", ".")
    git(upstream, "commit", "-q", "-m", "big")
    small = settings.model_copy(update={"max_repo_mb": 1})
    registry, builder = _make(small)
    registry.add("demo", upstream.as_uri(), "main")
    builder.build_now("demo")
    p = registry.get("demo")
    assert p.status == FAILED and "HUB_MAX_REPO_MB" in p.last_error
    assert not builder.repo_dir("demo").exists()


def test_reregistering_a_name_never_reuses_old_output(settings, upstream, tmp_path: Path):
    registry, builder = _make(settings)
    registry.add("demo", upstream.as_uri(), "main")
    builder.build_now("demo")
    stale = builder.graph_dir("demo") / "stale-marker"
    stale.write_text("x")

    registry.remove("demo")
    registry.add("demo", upstream.as_uri(), "main")
    builder.build_now("demo")
    assert registry.get("demo").status == READY
    assert not stale.exists()


def test_restart_resumes_interrupted_builds(settings, upstream):
    registry, _ = _make(settings)
    registry.add("demo", upstream.as_uri(), "main")
    registry.update("demo", status="building")
    registry.close()

    registry, builder = _make(settings)
    assert registry.reset_interrupted() == ["demo"]
    assert registry.get("demo").status == PENDING
