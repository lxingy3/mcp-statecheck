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


@pytest.mark.parametrize(
    ("source", "loader", "runner"),
    (
        ("mcp-v2.toml", matrix._load_runners, "python-v1"),
        ("mcp-modern.toml", matrix._load_modern_runners, "python-v2"),
    ),
)
def test_matrix_configs_reject_duplicate_runner_ids(
    tmp_path: Path,
    source: str,
    loader: object,
    runner: str,
) -> None:
    config = tmp_path / source
    original = (Path(__file__).parents[1] / "benchmarks" / source).read_text(
        encoding="utf-8"
    )
    config.write_text(
        original
        + "\n[[runners]]\n"
        + f'id = "{runner}"\n'
        + 'runtime = "duplicate"\n'
        + 'package = "duplicate"\n'
        + 'version = "0"\n',
        encoding="utf-8",
    )

    with pytest.raises(matrix.MatrixInfrastructureError, match="runner IDs"):
        loader(config)  # type: ignore[operator]


def test_matrix_cli_dispatches_the_package_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "matrix.toml"
    config.write_text("schema_version = 1\n", encoding="utf-8")
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


def test_matrix_cli_selects_the_bundled_modern_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "modern"
    observed: list[tuple[Path | None, Path]] = []

    def run(config_path: Path | None, output_path: Path) -> list[Path]:
        observed.append((config_path, output_path))
        return [output_path / f"{index}.json" for index in range(4)]

    monkeypatch.setattr(matrix, "run_matrix", run)

    assert cli.main(["matrix", "--profile", "modern", "--output", str(output)]) == 0
    assert observed == [(matrix._modern_config(), output)]
    assert capsys.readouterr().out == (
        "Matrix passed: wrote 4 locked SDK transport traces\n"
    )


def test_matrix_cli_resolves_a_custom_modern_profile_in_check_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "modern.toml"
    config.write_text(
        'schema_version = 1\naction_profile = "modern-stateless"\n',
        encoding="utf-8",
    )
    output = tmp_path / "modern"
    observed: list[tuple[Path | None, Path]] = []

    def check(config_path: Path | None, output_path: Path) -> None:
        observed.append((config_path, output_path))

    monkeypatch.setattr(matrix, "check_matrix", check)

    assert cli.main(["matrix", str(config), "--check", "--output", str(output)]) == 0
    assert observed == [(config, output)]
    assert capsys.readouterr().out == (
        "Matrix passed: 4/4 locked SDK transport cells match artifacts\n"
    )


def test_matrix_cli_rejects_profile_and_config_together(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "matrix.toml"
    config.write_text("schema_version = 1\n", encoding="utf-8")

    assert cli.main(["matrix", str(config), "--profile", "legacy"]) == 2
    assert "--profile cannot be combined with a config path" in capsys.readouterr().err


def test_matrix_cli_handles_a_missing_bundled_modern_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing() -> Path:
        raise matrix.MatrixInfrastructureError("bundled modern config is missing")

    monkeypatch.setattr(matrix, "_modern_config", missing)

    assert cli.main(["matrix", "--profile", "modern"]) == 2
    assert "bundled modern config is missing" in capsys.readouterr().err


def test_matrix_cli_uses_separate_default_output_for_modern_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[Path] = []

    def run(_config: Path | None, output: Path) -> list[Path]:
        observed.append(output)
        return [output / f"{index}.json" for index in range(4)]

    monkeypatch.setattr(matrix, "run_matrix", run)

    assert cli.main(["matrix", "--profile", "modern"]) == 0
    assert observed == [Path("artifacts/m6/matrix")]


def test_matrix_module_entry_reports_custom_modern_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "modern.toml"
    config.write_text('action_profile = "modern-stateless"\n', encoding="utf-8")
    output = tmp_path / "matrix"
    observed: list[tuple[Path | None, Path]] = []

    def check(config_path: Path | None, output_path: Path) -> None:
        observed.append((config_path, output_path))

    monkeypatch.setattr(matrix, "check_matrix", check)

    assert matrix.script_main([str(config), "--check", "--output", str(output)]) == 0
    assert observed == [(config, output)]
    assert capsys.readouterr().out == (
        "M6.1 client matrix passed: 4/4 real SDK transport cells match artifacts\n"
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


def test_cleanup_timeout_starts_after_the_hanging_call_is_observed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeTransport:
        returncode = 1

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> FakeTransport:
            events.append("enter")
            return self

        async def __aexit__(self, *_args: object) -> None:
            events.append("exit")

        async def send(self, _message: object) -> None:
            events.append("send")

        async def receive(self) -> None:
            events.append("receive")
            raise matrix.StdioTimeout("expected hang")

    runtime = matrix._MatrixRuntime(
        workdir=tmp_path,
        import_root=tmp_path,
        python_environments={},
        typescript_environments={},
        typescript_runner=tmp_path / "typescript_client.mts",
    )
    request = matrix.Envelope(command_id="cleanup-probe", kind="run", payload={})
    monkeypatch.setattr(matrix, "StdioTransport", FakeTransport)
    monkeypatch.setattr(
        matrix,
        "_adapter_command",
        lambda _runner_id, _runtime: (["adapter"], {}),
    )

    async def await_reached() -> None:
        events.append("reached")

    async def probe() -> None:
        await matrix._expect_adapter_timeout(
            request,
            "python-v2",
            runtime,
            await_reached=await_reached,
        )

    matrix.anyio.run(probe)

    assert events == ["enter", "send", "reached", "receive", "exit"]


def test_cleanup_reach_wait_accepts_a_valid_method_prefix() -> None:
    expected = ("server/discover", "tools/list", "tools/call")
    observations = iter((expected[:1], expected))

    async def wait() -> None:
        await matrix._wait_for_cleanup_hang(
            lambda: next(observations),
            expected,
            runner_id="python-v2",
        )

    matrix.anyio.run(wait)


def test_cleanup_reach_wait_rejects_an_invalid_method_order() -> None:
    async def wait() -> None:
        await matrix._wait_for_cleanup_hang(
            lambda: ("tools/list",),
            ("server/discover", "tools/list", "tools/call"),
            runner_id="python-v2",
        )

    with pytest.raises(matrix.MatrixFailure, match="observed unexpected methods"):
        matrix.anyio.run(wait)


def test_cleanup_reach_wait_has_an_independent_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(matrix, "CLEANUP_REACH_TIMEOUT", 0.01)

    async def wait() -> None:
        await matrix._wait_for_cleanup_hang(
            lambda: (),
            ("server/discover", "tools/list", "tools/call"),
            runner_id="python-v2",
        )

    with pytest.raises(matrix.MatrixFailure, match="did not reach the hanging call"):
        matrix.anyio.run(wait)


@pytest.mark.parametrize(
    ("times_out", "returncode", "message"),
    (
        (False, 1, "did not reach its hard timeout"),
        (True, 0, "did not reap its adapter"),
    ),
)
def test_cleanup_probe_rejects_a_response_or_unreaped_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    times_out: bool,
    returncode: int,
    message: str,
) -> None:
    class FakeTransport:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.returncode = returncode

        async def __aenter__(self) -> FakeTransport:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def send(self, _message: object) -> None:
            pass

        async def receive(self) -> None:
            if times_out:
                raise matrix.StdioTimeout("expected hang")

    runtime = matrix._MatrixRuntime(
        workdir=tmp_path,
        import_root=tmp_path,
        python_environments={},
        typescript_environments={},
        typescript_runner=tmp_path / "typescript_client.mts",
    )
    request = matrix.Envelope(command_id="cleanup-probe", kind="run", payload={})
    monkeypatch.setattr(matrix, "StdioTransport", FakeTransport)
    monkeypatch.setattr(
        matrix,
        "_adapter_command",
        lambda _runner_id, _runtime: (["adapter"], {}),
    )

    async def probe() -> None:
        await matrix._expect_adapter_timeout(request, "python-v2", runtime)

    with pytest.raises(matrix.MatrixFailure, match=message):
        matrix.anyio.run(probe)


def test_cleanup_probe_does_not_treat_a_send_timeout_as_a_hang(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTransport:
        returncode = 1

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> FakeTransport:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def send(self, _message: object) -> None:
            raise matrix.StdioTimeout("send timed out")

        async def receive(self) -> None:
            raise AssertionError("receive must not run after a send timeout")

    runtime = matrix._MatrixRuntime(
        workdir=tmp_path,
        import_root=tmp_path,
        python_environments={},
        typescript_environments={},
        typescript_runner=tmp_path / "typescript_client.mts",
    )
    request = matrix.Envelope(command_id="cleanup-probe", kind="run", payload={})
    monkeypatch.setattr(matrix, "StdioTransport", FakeTransport)
    monkeypatch.setattr(
        matrix,
        "_adapter_command",
        lambda _runner_id, _runtime: (["adapter"], {}),
    )

    async def probe() -> None:
        await matrix._expect_adapter_timeout(request, "python-v2", runtime)

    with pytest.raises(matrix.StdioTimeout, match="send timed out"):
        matrix.anyio.run(probe)


def test_live_peer_report_retries_a_windows_sharing_violation() -> None:
    class SharingReport:
        attempts = 0

        def read_text(self, *, encoding: str) -> str:
            assert encoding == "utf-8"
            self.attempts += 1
            if self.attempts == 1:
                raise PermissionError("file is being replaced")
            return '{"methods":["server/discover","tools/list","tools/call"]}'

    report = SharingReport()

    assert matrix._live_report_methods(report) == ()  # type: ignore[arg-type]
    assert matrix._live_report_methods(report) == (  # type: ignore[arg-type]
        "server/discover",
        "tools/list",
        "tools/call",
    )


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
