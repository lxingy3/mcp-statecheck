from __future__ import annotations

from pathlib import Path

import pytest

import mcp_statecheck.cli as cli
import mcp_statecheck.matrix as matrix


def test_runtime_materialization_copies_only_locked_inputs(tmp_path: Path) -> None:
    runtime = matrix._materialize_runtime(tmp_path)

    assert {
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == {
        "adapter/mcp_statecheck/__init__.py",
        "adapter/mcp_statecheck/adapters/__init__.py",
        "adapter/mcp_statecheck/adapters/jsonl.py",
        "adapter/mcp_statecheck/adapters/python_client.py",
        "adapter/mcp_statecheck/model.py",
        "python/v1/pyproject.toml",
        "python/v1/uv.lock",
        "python/v2/pyproject.toml",
        "python/v2/uv.lock",
        "typescript/v1/package-lock.json",
        "typescript/v1/package.json",
        "typescript/v2/package-lock.json",
        "typescript/v2/package.json",
        "typescript_client.mts",
    }
    assert runtime.import_root == tmp_path / "adapter"
    assert runtime.typescript_runner.is_file()
    assert not tuple(tmp_path.rglob(".venv"))
    assert not tuple(tmp_path.rglob("node_modules"))


def test_matrix_environment_clears_isolation_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in matrix._ISOLATION_VARIABLES:
        monkeypatch.setenv(name.swapcase(), "must-not-leak")
    monkeypatch.setenv("MCP_STATECHECK_KEEP", "kept")

    environment = matrix._isolated_environment()

    blocked = {name.casefold() for name in matrix._ISOLATION_VARIABLES}
    assert not any(name.casefold() in blocked for name in environment)
    assert environment["MCP_STATECHECK_KEEP"] == "kept"


def test_default_matrix_config_is_the_locked_16_cell_benchmark() -> None:
    runners = matrix._load_runners(matrix._default_config())

    assert set(runners) == set(matrix.RUNNER_IDS)
    assert (
        len(matrix.RUNNER_IDS) * len(matrix.PROTOCOL_VERSIONS) * len(matrix.TRANSPORTS)
        == 16
    )


def test_matrix_cli_dispatches_the_package_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "matrix.toml"
    output = tmp_path / "output"
    observed: list[tuple[Path | None, Path]] = []

    def run(config_path: Path | None, output_path: Path) -> list[Path]:
        observed.append((config_path, output_path))
        return [output_path / f"{index}.json" for index in range(16)]

    monkeypatch.setattr(matrix, "run_matrix", run)

    assert cli.main(["matrix", str(config), "--output", str(output)]) == 0
    assert observed == [(config, output)]
    assert capsys.readouterr().out == (
        "Matrix passed: wrote 16 locked SDK transport traces\n"
    )


@pytest.mark.parametrize(
    ("error", "expected_exit"),
    (
        (matrix.MatrixFailure("different trace"), 1),
        (matrix.MatrixInfrastructureError("missing runtime"), 2),
    ),
)
def test_matrix_cli_preserves_failure_vs_infrastructure_exit_codes(
    error: matrix.MatrixError,
    expected_exit: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(_config: Path | None, _output: Path) -> list[Path]:
        raise error

    monkeypatch.setattr(matrix, "run_matrix", fail)

    assert cli.main(["matrix"]) == expected_exit
    captured = capsys.readouterr()
    assert not captured.out
    assert str(error) in captured.err


def test_matrix_check_requires_an_existing_artifact_directory(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        matrix.MatrixInfrastructureError,
        match="expected artifact directory does not exist",
    ):
        matrix.check_matrix(None, tmp_path / "missing")


def test_adapter_spawn_failure_is_infrastructure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = matrix._materialize_runtime(tmp_path / "runtime")
    request = matrix.Envelope(command_id="missing-adapter", kind="run", payload={})
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(
        matrix,
        "_adapter_command",
        lambda _runner_id, _runtime: ([str(missing)], {}),
    )

    with pytest.raises(
        matrix.MatrixInfrastructureError,
        match="adapter could not start",
    ):
        matrix.anyio.run(matrix._exchange, request, "python-v1", runtime)


def test_started_sdk_timeout_is_a_compatibility_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = matrix._materialize_runtime(tmp_path / "runtime")
    request = matrix.Envelope(command_id="hanging-adapter", kind="run", payload={})
    monkeypatch.setattr(
        matrix,
        "_adapter_command",
        lambda _runner_id, _runtime: (
            [matrix.sys.executable, "-c", "import time; time.sleep(0.2)"],
            {},
        ),
    )

    async def exchange() -> None:
        await matrix._exchange(request, "python-v1", runtime, timeout=0.05)

    with pytest.raises(
        matrix.MatrixFailure,
        match="SDK cell exceeded its hard timeout",
    ):
        matrix.anyio.run(exchange)


def test_structured_sdk_failure_is_a_compatibility_failure() -> None:
    response = matrix.Envelope(
        command_id="failed-cell",
        kind="failure",
        payload={
            "error_type": "RuntimeError",
            "message": "controlled SDK failure",
            "runner_id": "python-v1",
        },
    )

    with pytest.raises(matrix.MatrixFailure, match="controlled SDK failure"):
        matrix._result_payload(
            response,
            command_id="failed-cell",
            runner_id="python-v1",
            runner={"version": "1.28.1"},
        )
