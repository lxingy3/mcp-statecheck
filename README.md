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
uv build
```

The lockfile selects CPython 3.12. Tests must not use another interpreter.

## License

Apache-2.0.

