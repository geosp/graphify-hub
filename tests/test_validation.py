import pytest

from graphify_hub import validation as v
from graphify_hub.config import HubSettings

GH = ["https://github.com/"]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://github.com/geosp/praxismesh", "https://github.com/geosp/praxismesh.git"),
        ("https://github.com/geosp/praxismesh.git", "https://github.com/geosp/praxismesh.git"),
        ("https://GitHub.com/geosp/praxismesh/", "https://github.com/geosp/praxismesh.git"),
        ("git@github.com:geosp/praxismesh.git", "https://github.com/geosp/praxismesh.git"),
    ],
)
def test_repo_url_canonical(given, expected):
    assert v.repo_url(given, GH) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "file:///etc",
        "ssh://git@github.com/geosp/praxismesh.git",
        "http://github.com/geosp/praxismesh",
        "ext::sh -c touch% /tmp/pwned",
        "https://user:pw@github.com/geosp/praxismesh",
        "https://github.com:8443/geosp/praxismesh",
        "https://github.com/geosp/praxismesh?x=1",
        "https://github.com/geosp/../etc/passwd",
        "https://github.com/geosp",
        "https://gitlab.com/geosp/praxismesh",
        "--upload-pack=touch /tmp/x",
        "",
    ],
)
def test_repo_url_rejected(bad):
    with pytest.raises(v.ValidationError):
        v.repo_url(bad, GH)


def test_repo_url_respects_narrow_allowlist():
    only = ["https://github.com/geosp/"]
    assert v.repo_url("https://github.com/geosp/x", only)
    with pytest.raises(v.ValidationError):
        v.repo_url("https://github.com/someone-else/x", only)


@pytest.mark.parametrize("good", ["praxismesh", "a", "my-app-2", "x" * 63])
def test_project_name_ok(good):
    assert v.project_name(good) == good


@pytest.mark.parametrize("bad", ["", "-x", "Praxis", "a_b", "../x", "a/b", "x" * 64])
def test_project_name_rejected(bad):
    with pytest.raises(v.ValidationError):
        v.project_name(bad)


@pytest.mark.parametrize("good", ["main", "v8", "feat/92-phase-3a", "release-1.2"])
def test_branch_ok(good):
    assert v.branch_name(good) == good


@pytest.mark.parametrize("bad", ["", "-x", "--upload-pack=x", "a..b", "a b", "x/", "x.lock"])
def test_branch_rejected(bad):
    with pytest.raises(v.ValidationError):
        v.branch_name(bad)


def test_allowlist_setting_requires_https():
    with pytest.raises(ValueError):
        HubSettings(allowed_repos="file:///")
    assert HubSettings(allowed_repos=" https://a/ , https://b/ ").allowed_prefixes == [
        "https://a/",
        "https://b/",
    ]
