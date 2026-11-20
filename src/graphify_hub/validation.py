"""Checks on everything a client sends before it reaches git or the filesystem."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SCP = re.compile(r"^git@(?P<host>[A-Za-z0-9.-]+):(?P<path>[A-Za-z0-9._/-]+)$")
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


class ValidationError(ValueError):
    """A client argument the hub refuses."""


def project_name(name: str) -> str:
    if not _NAME.fullmatch(name or ""):
        raise ValidationError(
            "project name must be 1-63 chars of lowercase letters, digits and '-', "
            "starting with a letter or digit"
        )
    return name


def branch_name(branch: str) -> str:
    if (
        not _BRANCH.fullmatch(branch or "")
        or branch.startswith(("-", "/"))
        or branch.endswith(("/", ".lock"))
        or ".." in branch
        or "//" in branch
    ):
        raise ValidationError(f"invalid branch name: {branch!r}")
    return branch


def repo_url(url: str, allowed_prefixes: list[str]) -> str:
    """Return the canonical https URL for ``url`` or raise ``ValidationError``.

    ``git@host:owner/repo`` is rewritten to ``https://host/owner/repo``. Only
    https is accepted, with no credentials, port, query or fragment, and the
    result must start with one of ``allowed_prefixes``.
    """
    url = (url or "").strip()
    scp = _SCP.fullmatch(url)
    if scp:
        url = f"https://{scp.group('host')}/{scp.group('path')}"
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValidationError("only https:// repository URLs are accepted (public repos)")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ValidationError("repository URLs must not contain credentials")
    if parts.port is not None or parts.query or parts.fragment or not parts.hostname:
        raise ValidationError("repository URL must be https://host/owner/repo")
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2 or any(s in (".", "..") or not _SEGMENT.fullmatch(s) for s in segments):
        raise ValidationError("repository URL must be https://host/owner/repo")
    if segments[-1].endswith(".git"):
        segments[-1] = segments[-1][: -len(".git")]
    canonical = f"https://{parts.hostname.lower()}/{'/'.join(segments)}.git"
    if not any(canonical.startswith(prefix) for prefix in allowed_prefixes):
        raise ValidationError(f"repository URL is not in HUB_ALLOWED_REPOS: {canonical}")
    return canonical
