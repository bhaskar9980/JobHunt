"""`jobhunt doctor`, `jobhunt backup` and `jobhunt plan` against a temp root."""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import typer
from typer.testing import CliRunner

from jobhunt import cli_ops

PROJECT_ROOT = Path(__file__).parent.parent.parent
runner = CliRunner()


@pytest.fixture
def app():
    test_app = typer.Typer()
    test_app.command("plan")(cli_ops.plan_cmd)
    test_app.command("doctor")(cli_ops.doctor_cmd)
    test_app.command("backup")(cli_ops.backup_cmd)
    return test_app


@pytest.fixture
def root(tmp_path, monkeypatch):
    shutil.copytree(
        PROJECT_ROOT / "config",
        tmp_path / "config",
        ignore=shutil.ignore_patterns("answers.yaml", "profile.yaml"),
    )
    config = tmp_path / "config"
    shutil.copy(config / "answers.example.yaml", config / "answers.yaml")
    shutil.copy(config / "profile.example.yaml", config / "profile.yaml")
    monkeypatch.setenv("JOBHUNT_ROOT", str(tmp_path))
    monkeypatch.setenv("JOBHUNT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JOBHUNT_DB_PATH", str(tmp_path / "data" / "jobs.db"))
    (tmp_path / "data").mkdir()
    return tmp_path


def test_doctor_lists_every_check_and_flags_setup_gaps(app, root):
    result = runner.invoke(app, ["doctor", "--json"])
    checks = {c["name"]: c for c in json.loads(result.output)}
    for name in (
        "profile.yaml",
        "companies.yaml",
        "sources.yaml",
        "referrals.yaml",
        "mail_rules.yaml",
        "tailor.yaml",
        "ats/registry.yaml",
        "resume_pdf",
        "answers.yaml",
        "pdflatex",
        "chrome",
        "playwright mcp",
        "database",
        "contacts",
        "last scan",
        "sources failing",
        "mail sync",
    ):
        assert name in checks, name
    assert all(checks[n]["ok"] for n in ("profile.yaml", "companies.yaml", "database"))
    assert checks["contacts"]["ok"] is False and checks["contacts"]["severity"] == "warn"
    assert checks["last scan"]["ok"] is False
    assert checks["mail sync"]["ok"] is False
    assert checks["playwright mcp"]["ok"] is False  # no node_modules under the temp root
    hard_failures = [c for c in checks.values() if not c["ok"] and c["severity"] == "fail"]
    assert result.exit_code == (1 if hard_failures else 0)


def test_doctor_fails_when_answers_missing(app, root):
    (root / "config" / "answers.yaml").unlink()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "FAIL answers.yaml" in result.output


def test_backup_writes_db_copy_and_archive(app, root):
    (root / "data" / "applications" / "7").mkdir(parents=True)
    (root / "data" / "applications" / "7" / "run.log").write_text("hello", encoding="utf-8")
    (root / "data" / "resume_facts.md").write_text("# facts", encoding="utf-8")
    target = root / "bk"
    result = runner.invoke(app, ["backup", "--to", str(target)])
    assert result.exit_code == 0, result.output
    dbs = list(target.glob("jobs-*.db"))
    zips = list(target.glob("jobhunt-*.zip"))
    assert len(dbs) == 1 and len(zips) == 1
    with sqlite3.connect(dbs[0]) as copy:
        assert copy.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    names = zipfile.ZipFile(zips[0]).namelist()
    assert any(n.endswith("run.log") for n in names)
    assert any(n.endswith("resume_facts.md") for n in names)
    assert any(n.endswith("answers.yaml") for n in names)


def test_plan_command_renders_and_json(app, root):
    text = runner.invoke(app, ["plan", "--no-referrals"])
    assert text.exit_code == 0, text.output
    assert "# Today" in text.output
    payload = json.loads(runner.invoke(app, ["plan", "--json", "--no-referrals"]).output)
    assert set(payload) >= {
        "ready_to_apply",
        "referral_recommended",
        "needs_input",
        "progress",
        "health",
    }


@pytest.mark.parametrize("failed", [False, True])
def test_scan_exit_reports_source_failures_without_losing_summary(monkeypatch, failed):
    from jobhunt import cli, pipeline

    summary = pipeline.ScanSummary(
        sources=[
            pipeline.SourceStats(label="greenhouse:Acme", fetched=2, created=1),
            pipeline.SourceStats(label="lever:Other", error="mock unavailable" if failed else None),
        ]
    )
    ctx = SimpleNamespace(conn=object(), settings=object())
    scan = Mock(return_value=summary)
    monkeypatch.setattr(cli, "_ctx", Mock(return_value=ctx))
    monkeypatch.setattr(cli, "load_profile", Mock(return_value=object()))
    monkeypatch.setattr(cli, "title_predicate", Mock(return_value=None))
    monkeypatch.setattr(cli.pipeline, "scan", scan)

    result = runner.invoke(cli.app, ["scan", "--no-hydrate"])

    assert result.exit_code == (1 if failed else 0), result.output
    assert "greenhouse:Acme" in result.output
    assert "lever:Other" in result.output
    assert f"scan done: created=1 merged=0 hydrated=0 errors={int(failed)}" in result.output
    if failed:
        assert "ERROR mock unavailable" in result.output
    scan.assert_called_once_with(
        ctx.conn,
        ctx.settings,
        only_source=None,
        only_company=None,
        hydrate=False,
        dry_run=False,
        should_hydrate=None,
    )


@pytest.fixture(params=["powershell.exe", "pwsh"])
def powershell(request):
    executable = shutil.which(request.param)
    if executable is None:
        pytest.skip(f"{request.param} is not installed")
    return executable


def _run_scheduler_fragment(powershell, tmp_path, body):
    script = (PROJECT_ROOT / "scripts" / "daily_scan.ps1").read_text(encoding="utf-8-sig")
    harness = (
        r"""
$ErrorActionPreference = "Continue"
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput(
    $env:JOBHUNT_TEST_SCRIPT, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
$steps = [ordered]@{}
$log = Join-Path $env:JOBHUNT_TEST_ROOT "mock.log"
$statusFile = Join-Path $env:JOBHUNT_TEST_ROOT "last_scan.json"
$started = Get-Date -Format o
$claude = "mock-missing-claude.exe"
function Out-File {
    param([Parameter(ValueFromPipeline=$true)]$InputObject, $FilePath, [switch]$Append, $Encoding)
    process {
        if ($FilePath -eq $statusFile) {
            Microsoft.PowerShell.Utility\Out-File -InputObject $InputObject `
                -FilePath $FilePath -Encoding $Encoding
        }
    }
}
$definition = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq "Run-Step"
}, $true)
Invoke-Expression $definition.Extent.Text
"""
        + body
        + r"""
$summaryStart = $ast.EndBlock.Statements | Where-Object { $_.Extent.Text -match '^\$failed\s*=' }
if ($null -eq $summaryStart) { throw "scheduler summary not found" }
$summaryStatements = $ast.EndBlock.Statements | Where-Object {
    $_.Extent.StartOffset -ge $summaryStart.Extent.StartOffset
}
Invoke-Expression (($summaryStatements | ForEach-Object { $_.Extent.Text }) -join "`n")
"""
    )
    encoded = base64.b64encode(harness.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        env={
            **os.environ,
            "JOBHUNT_TEST_SCRIPT": script,
            "JOBHUNT_TEST_ROOT": str(tmp_path),
            "JOBHUNT_TEST_PYTHON": sys.executable,
        },
        capture_output=True,
        timeout=30,
    )
    status = tmp_path / "last_scan.json"
    assert status.exists(), result.stderr.decode(errors="replace")
    return result.returncode, status


@pytest.mark.parametrize(
    ("block", "exit_code"),
    [
        ("'mock success'", 0),
        ("throw 'mock exception'", 1),
        ("Write-Error 'mock nonterminating error'", 1),
        ("Get-Item 'TestDrive-does-not-exist:/missing'", 1),
        ("& $env:JOBHUNT_TEST_PYTHON -I -S -c 'import sys; sys.exit(7)'", 7),
        (
            "& $env:JOBHUNT_TEST_PYTHON -I -S -c "
            "'import sys; sys.stderr.write(str(123)); sys.exit(0)'",
            0,
        ),
        (
            "& $env:JOBHUNT_TEST_PYTHON -I -S -c "
            "'import sys; sys.stderr.write(str(123)); sys.exit(7)'",
            7,
        ),
        ("& (Join-Path $env:JOBHUNT_TEST_ROOT 'absent.exe')", 1),
    ],
)
def test_scheduler_step_failures_and_stale_native_codes(powershell, tmp_path, block, exit_code):
    code, status = _run_scheduler_fragment(
        powershell,
        tmp_path,
        f"""
$global:LASTEXITCODE = 23
Run-Step "scan" {{ {block} }}
Run-Step "dashboard" {{ "mock dashboard success" }}
""",
    )
    payload = json.loads(status.read_text(encoding="utf-8-sig"))
    assert payload["steps"]["scan"] == exit_code
    assert payload["steps"]["dashboard"] == 0
    assert payload["ok"] is (exit_code == 0)
    assert payload["failed"] == (["scan"] if exit_code else [])
    assert payload["skipped"] == []
    assert code == (1 if exit_code else 0)


def test_scheduler_missing_claude_is_unsuccessful_and_explicitly_skipped(powershell, tmp_path):
    code, status = _run_scheduler_fragment(
        powershell,
        tmp_path,
        r"""
Run-Step "scan" { "mock success" }
$branch = $ast.EndBlock.Statements | Where-Object {
    $_ -is [System.Management.Automation.Language.IfStatementAst] -and
    $_.Extent.Text -match 'Test-Path \$claude'
}
if ($null -eq $branch) { throw "missing executable branch not found" }
Invoke-Expression (($branch.ElseClause.Statements | ForEach-Object { $_.Extent.Text }) -join "`n")
Run-Step "dashboard" { "mock dashboard success" }
""",
    )
    payload = json.loads(status.read_text(encoding="utf-8-sig"))
    assert payload["ok"] is False
    assert code == 1
    assert payload["skipped"] == ["claude_scan"]
    assert payload["steps"]["claude_scan"].startswith("skipped:")
    assert "not found" in payload["steps"]["claude_scan"]
    assert payload["steps"]["dashboard"] == 0


def test_scheduler_status_json_is_plain_utf8_for_python(powershell, tmp_path):
    code, status = _run_scheduler_fragment(
        powershell,
        tmp_path,
        """
Run-Step "scan" { "mock success" }
""",
    )
    assert code == 0
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["steps"] == {"scan": 0}


def test_dashboard_progress_cli_and_failed_summary(root):
    from jobhunt import cli, dashboard
    from jobhunt.config import load_settings

    result = runner.invoke(cli.app, ["dashboard-progress", "health", "--new-run"])
    assert result.exit_code == 0, result.output
    assert (
        runner.invoke(cli.app, ["dashboard-progress", "health", "--state", "failed"]).exit_code == 0
    )
    assert (
        runner.invoke(cli.app, ["dashboard-progress", "finish", "--state", "done"]).exit_code == 0
    )
    progress = dashboard.workflow_state(load_settings())
    assert progress["status"] == "failed"
    assert progress["steps"]["health"]["state"] == "failed"
    assert runner.invoke(cli.app, ["dashboard-progress", "fake-stage"]).exit_code != 0


def test_failed_score_import_has_nonzero_exit(root, monkeypatch):
    from jobhunt import cli, scoring

    monkeypatch.setattr(
        scoring, "import_batch", lambda *args: scoring.ImportSummary(errors=["invalid result"])
    )
    result = runner.invoke(cli.app, ["score-batch", "--import", "ignored.json"])
    assert result.exit_code != 0
    assert "invalid result" in result.output


def test_failed_alert_ingest_has_nonzero_exit(root, monkeypatch):
    from jobhunt import cli
    from jobhunt.sources import gmail_alerts

    monkeypatch.setattr(
        gmail_alerts,
        "ingest_inbox",
        lambda *args: gmail_alerts.IngestSummary(failed=["invalid file"]),
    )
    result = runner.invoke(cli.app, ["ingest-inbox"])
    assert result.exit_code != 0
    assert "invalid file" in result.output


def test_mail_lookback_override_is_used_in_queries(root):
    from jobhunt import cli

    result = runner.invoke(cli.app, ["mail", "last-sync", "--json", "--days", "14"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["lookback_days"] == 14
    assert data["search_after"]
    assert all("newer_than:14d" in q for q in data["queries"].values())


def test_serve_cli_respects_zero_port_and_no_open(root, monkeypatch):
    from jobhunt import cli

    serve = Mock(return_value="http://127.0.0.1:1234/")
    monkeypatch.setattr(cli_ops.server, "serve", serve)
    result = runner.invoke(cli.app, ["serve", "--port", "0", "--no-open"])
    assert result.exit_code == 0, result.output
    assert serve.call_args.kwargs["port"] == 0
    assert serve.call_args.kwargs["open_browser"] is False


def test_day_skill_opens_dashboard_before_health_or_scan():
    skill = (PROJECT_ROOT / ".claude" / "skills" / "jobs-day" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert skill.index("uv run jobhunt serve") < skill.index("uv run jobhunt doctor")
    assert "dashboard-progress health --state running --new-run" in skill
    assert "uv run jobhunt dashboard-progress finish --state done" in skill
    assert "reuses it and exits successfully" in skill
