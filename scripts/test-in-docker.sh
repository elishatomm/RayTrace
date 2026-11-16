#!/usr/bin/env bash
# Run the Python test suite in a throwaway Linux container.
#
#   scripts/test-in-docker.sh [pytest args]     e.g. scripts/test-in-docker.sh tests/test_world.py -x
#
# The image holds its own copy of the checkout (see Dockerfile.test and .dockerignore), so nothing
# from the host is mounted and nothing here is written. At test time the container has no network,
# a read-only root filesystem and an in-memory /tmp, so every database a test creates disappears
# with the container. Ollama tests need a model server and are always deselected.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="recovery-workbench-test:local"

docker build --file "$repo_root/Dockerfile.test" --tag "$image" "$repo_root"

exec docker run --rm --init --network none --read-only --tmpfs /tmp \
    "$image" -q -m "not ollama" "$@"
