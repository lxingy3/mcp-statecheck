import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNERS = {
    "python-v1": ("mcp", "1.28.1", "3.12.13"),
    "python-v2": ("mcp", "2.0.0rc1", "3.12.13"),
    "typescript-v1": ("@modelcontextprotocol/sdk", "1.30.0", "24.14.1"),
    "typescript-v2": (
        "@modelcontextprotocol/client",
        "2.0.0",
        "24.14.1",
    ),
}
PROTOCOLS = ("2025-06-18", "2025-11-25")
TRANSPORTS = ("stdio", "streamable-http")


def _expected_events(
    protocol: str,
    transport: str,
) -> list[dict[str, object]]:
    observation: dict[str, object] = {
        "initialize_protocol_version": "2025-11-25",
        "kind": "peer_observation",
        "method_order": [
            "initialize",
            "notifications/initialized",
            "ping",
            "tools/list",
            "tools/call",
        ],
        "negotiated_protocol_version": protocol,
    }
    if transport == "streamable-http":
        observation.update(
            {
                "post_accept_headers_valid": True,
                "protocol_headers_preserved": True,
                "session_deleted": True,
                "session_preserved": True,
            }
        )
    return [
        {
            "kind": "response",
            "method": "initialize",
            "protocol_version": protocol,
            "server_info": {"name": "controlled-peer", "version": "0.1"},
            "target_action_id": "initialize",
        },
        {
            "kind": "response",
            "method": "ping",
            "target_action_id": "ping",
        },
        {
            "kind": "response",
            "method": "tools/list",
            "target_action_id": "list-tools",
            "tool_names": ["echo"],
        },
        {
            "is_error": False,
            "kind": "response",
            "method": "tools/call",
            "target_action_id": "call-echo",
            "text": "hello",
        },
        observation,
    ]


def test_m3_pins_cover_the_real_client_transport_matrix():
    with (ROOT / "benchmarks" / "mcp-v2.toml").open("rb") as handle:
        config = tomllib.load(handle)
    runners = {runner["id"]: runner for runner in config["runners"]}

    assert config["schema_version"] == 1
    assert tuple(config["protocol_versions"]) == PROTOCOLS
    assert tuple(config["transports"]) == TRANSPORTS
    assert set(runners) == set(RUNNERS)
    for runner_id, (package, version, _) in RUNNERS.items():
        assert runners[runner_id]["package"] == package
        assert runners[runner_id]["version"] == version

    assert runners["python-v2"]["dependencies"] == ["mcp-types==2.0.0rc1"]
    assert runners["python-v2"]["prerelease"] is True
    assert "prerelease" not in runners["typescript-v2"]
    for runner_id in ("python-v1", "python-v2"):
        environment = (
            ROOT
            / "src"
            / "mcp_statecheck"
            / "adapters"
            / "python"
            / runner_id.removeprefix("python-")
        )
        with (environment / "pyproject.toml").open("rb") as handle:
            manifest = tomllib.load(handle)
        lock_text = (environment / "uv.lock").read_text()
        with (environment / "uv.lock").open("rb") as handle:
            lock = tomllib.load(handle)
        package, version, _ = RUNNERS[runner_id]
        dependencies = [
            f"{package}=={version}",
            *runners[runner_id].get("dependencies", []),
        ]
        assert manifest["project"]["dependencies"] == dependencies
        assert lock["version"] == 1
        assert 'hash = "sha256:' in lock_text

    for runner_id in ("typescript-v1", "typescript-v2"):
        environment = (
            ROOT
            / "src"
            / "mcp_statecheck"
            / "adapters"
            / "typescript"
            / runner_id.removeprefix("typescript-")
        )
        manifest = json.loads((environment / "package.json").read_text())
        lock = json.loads((environment / "package-lock.json").read_text())
        package, version, runtime = RUNNERS[runner_id]
        assert manifest["engines"]["node"] == runtime
        assert manifest["dependencies"][package] == version
        assert lock["lockfileVersion"] == 3


def test_m3_checked_traces_are_complete_and_differentially_equal():
    directory = ROOT / "artifacts" / "m3"
    expected_names = {
        Path(transport) / f"{runner_id}-{protocol}.json"
        for transport in TRANSPORTS
        for runner_id in RUNNERS
        for protocol in PROTOCOLS
    }
    assert {
        path.relative_to(directory) for path in directory.rglob("*.json")
    } == expected_names

    for transport in TRANSPORTS:
        for runner_id, (_, sdk_version, runtime_version) in RUNNERS.items():
            for protocol in PROTOCOLS:
                artifact = json.loads(
                    (directory / transport / f"{runner_id}-{protocol}.json").read_text()
                )
                assert artifact["schema_version"] == 1
                assert artifact["adapter"] == runner_id
                assert artifact["sdk_version"] == sdk_version
                assert artifact["protocol_version"] == protocol
                assert artifact["transport"] == transport
                assert artifact["fixture_id"] == "sdk-client-smoke"
                assert artifact["generation"]["runtime_version"] == runtime_version
                if transport == "stdio":
                    assert artifact["cleanup"] == {
                        "adapter_reaped": True,
                        "adapter_returncode": 0,
                        "client_closed": True,
                        "peer_clean_exit": True,
                        "peer_reaped": True,
                    }
                else:
                    assert artifact["cleanup"] == {
                        "adapter_reaped": True,
                        "adapter_returncode": 0,
                        "client_closed": True,
                        "listener_closed": True,
                        "session_deleted": True,
                    }
                assert [
                    (action["kind"], action["method"])
                    for action in artifact["canonical_actions"]
                ] == [
                    ("connect", None),
                    ("initialize", None),
                    ("initialized", None),
                    ("request", "ping"),
                    ("request", "tools/list"),
                    ("request", "tools/call"),
                    ("close", None),
                ]

                events = [
                    {key: value for key, value in event.items() if key != "sequence"}
                    for event in artifact["normalized_events"]
                ]
                assert events == _expected_events(protocol, transport)
