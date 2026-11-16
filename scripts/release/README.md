# Release checks

The two tools behind [docs/RELEASE_VERIFICATION.md](../../docs/RELEASE_VERIFICATION.md). Both run in the test image, with no network, a read-only root and an in-memory `/tmp`:

```console
$ docker build -f Dockerfile.test -t recovery-workbench-test:local .
$ docker run --rm --init --network none --read-only --tmpfs /tmp:size=2g recovery-workbench-test:local \
    -p no:cacheprovider -m "not ollama" -q $(cat scripts/release/gate_tests.txt)
$ docker run --rm --init --network none --read-only --tmpfs /tmp:size=2g --entrypoint python \
    recovery-workbench-test:local scripts/release/demonstrations.py > demonstrations.json
```

- **`gate_tests.txt`** lists the tests the report maps to the release gates G01–G18, one pytest node ID per line. Most are existing tests. The gaps the mapping found are covered by `tests/test_release_gates.py`, where each docstring names its gate.
- **`demonstrations.py`** runs the three demonstrations through the workbench's own entry points:
  - a successful recovery;
  - an approval that becomes stale;
  - an unresolved payment state.

  It prints one JSON document with the IDs, effect counts, totals and receipt hashes that the report quotes. It uses rules mode only, and a listener on the model URL counts any model request.
