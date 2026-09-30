#!/bin/sh
# Run on the deployment host before Compose, using the same optional directory overrides.
set -eu
mkdir -p "${REPO_DIR:-./repo}" "${DATA_DIR:-./data}"
chown 10001:10001 "${REPO_DIR:-./repo}" "${DATA_DIR:-./data}"
chmod 700 "${REPO_DIR:-./repo}" "${DATA_DIR:-./data}"
