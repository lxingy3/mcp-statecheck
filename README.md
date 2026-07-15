# mcp-statecheck

`mcp-statecheck` is an experimental stateful differential test harness for
Model Context Protocol implementations.

The project is under private development. M0 establishes a reproducible Python
3.12 environment and records the protocol baseline. M1 adds the canonical
state model, stdio and Streamable HTTP wire transports, trace recording, and
controlled defect fixtures. Stateful generation, shrinking, replay, SDK
matrices, reports, and the CLI remain unfinished.

The repository will not be made public or released to PyPI until the v0.1
acceptance gate in [`docs/design.md`](docs/design.md) passes.

## Development

Install [uv](https://docs.astral.sh/uv/), then run:

```console
uv sync --locked
uv run pytest
uv run python scripts/run_m1_acceptance.py
uv build
```

The project pins CPython 3.12 through `.python-version`. Tests must not use
another interpreter, and `uv.lock` fixes the dependency graph.
The M1 command writes one real wire trace per controlled fixture under
`artifacts/m1/`. It does not run shrinking or the ten-replay v0.1 gate, which
belongs to M2.

## License

Apache-2.0.
