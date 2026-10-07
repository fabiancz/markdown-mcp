import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "release_metadata", Path(__file__).parents[1] / "scripts/release_metadata.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def environment(**updates):
    return dict(
        {
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF_TYPE": "tag",
            "GITHUB_REF_NAME": "v0.3.0rc1",
            "GITHUB_SHA": "a" * 40,
            "GITHUB_REPOSITORY": "fabiancz/markdown-mcp",
        },
        **updates,
    )


def test_release_candidate_is_distinct_and_never_promotes_latest():
    assert module.metadata("0.3.0rc1", environment()) == {
        "image": "ghcr.io/fabiancz/markdown-mcp",
        "tag": "0.3.0rc1",
        "latest": "false",
    }
    assert (
        module.metadata("0.3.0rc2", environment(GITHUB_REF_NAME="v0.3.0rc2"))["latest"] == "false"
    )
    with pytest.raises(ValueError, match="does not match"):
        module.metadata("0.3.0rc1", environment(GITHUB_REF_NAME="v0.2.3"))


def test_stable_tag_promotes_but_commit_builds_do_not():
    assert module.metadata("0.3.0", environment(GITHUB_REF_NAME="v0.3.0"))["latest"] == "true"
    for event in ["push", "pull_request"]:
        result = module.metadata(
            "0.3.0",
            environment(GITHUB_EVENT_NAME=event, GITHUB_REF_TYPE="branch", GITHUB_REF_NAME="main"),
        )
        assert result["tag"] == "sha-" + "a" * 12 and result["latest"] == "false"


@pytest.mark.parametrize("version,latest", [("0.3.0rc1", "false"), ("0.3.0", "true")])
def test_manual_dispatch_validates_requested_version(version, latest):
    env = environment(
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_REF_TYPE="branch",
        GITHUB_REF_NAME="main",
        REQUESTED_VERSION=version,
    )
    assert module.metadata(version, env) == {
        "image": "ghcr.io/fabiancz/markdown-mcp",
        "tag": version,
        "latest": latest,
    }
    with pytest.raises(ValueError, match="does not match"):
        module.metadata(version, dict(env, REQUESTED_VERSION="0.2.3"))


@pytest.mark.parametrize("version", ["0.03.0", "0.3.0rc0", "0.3.0rc01", "0.3.0-rc1", "latest"])
def test_invalid_package_versions_are_rejected(version):
    with pytest.raises(ValueError, match="Package version"):
        module.metadata(version, environment())
