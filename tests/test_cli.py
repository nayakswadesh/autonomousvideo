import json

from typer.testing import CliRunner

from autonomous_studio.cli import app


def test_cli_create_status_and_errors(tmp_path):
    runner = CliRunner()
    prefix = ["--root", str(tmp_path)]
    created = runner.invoke(app, [*prefix, "create", "examples/earthquake-countdown.yaml"])
    assert created.exit_code == 0, created.output
    project_id = json.loads(created.stdout)["id"]
    run = runner.invoke(app, [*prefix, "run", project_id])
    assert run.exit_code == 0, run.output
    assert json.loads(run.stdout)["state"] == "STORY_APPROVAL"
    status = runner.invoke(app, [*prefix, "status", project_id])
    assert json.loads(status.stdout)["pending"]["gate"] == "story"
    invalid = runner.invoke(app, [*prefix, "approve", project_id, "hero"])
    assert invalid.exit_code == 1
