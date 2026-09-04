from pathlib import Path

from mcp_statecheck.cli import main
from mcp_statecheck.reports import load_artifact


def test_tasks_cli_generates_replays_and_renders_a_real_failure(tmp_path: Path, capsys):
    output = tmp_path / "task.json"
    html = tmp_path / "task.html"
    junit = tmp_path / "task.xml"
    sarif = tmp_path / "task.sarif"
    assert (
        main(
            [
                "tasks",
                "--fixture",
                "task-result-shape",
                "--transport",
                "streamable-http",
                "--output",
                str(output),
                "--html",
                str(html),
                "--junit",
                str(junit),
                "--sarif",
                str(sarif),
            ]
        )
        == 1
    )
    assert "replay 10/10" in capsys.readouterr().out
    artifact = load_artifact(output)
    assert artifact["replay"]["matched"] == 10
    assert html.is_file() and junit.is_file() and sarif.is_file()
    assert main(["replay", str(output)]) == 1
    assert "10/10 attempts" in capsys.readouterr().err


def test_tasks_cli_rejects_colliding_outputs_before_generation(
    tmp_path, monkeypatch, capsys
):
    import mcp_statecheck.task_campaign as campaign

    def unexpected(*args, **kwargs):
        raise AssertionError("generation must not start")

    monkeypatch.setattr(campaign, "build_task_artifact", unexpected)
    path = str(tmp_path / "same.json")
    assert main(["tasks", "--output", path, "--html", path]) == 2
    assert "mcp-statecheck:" in capsys.readouterr().err
