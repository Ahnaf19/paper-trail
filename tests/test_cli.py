"""The CLI's own job: turning an IngestReport into an exit code and a log line."""

import pytest
from typer.testing import CliRunner

from app import cli
from app.services.ingest import IngestReport

runner = CliRunner()


@pytest.fixture
def report(monkeypatch):
    """Hand the CLI a prepared report — the loop itself is covered by test_ingest.py."""

    def _install(**fields: object) -> None:
        monkeypatch.setattr(cli, "run_ingest", lambda *a, **k: IngestReport(**fields))

    return _install


def test_an_empty_feed_exits_non_zero(report):
    """arXiv answers a mistyped category exactly as it answers a real empty one, so a
    zero-page run must never look like the green success it used to."""
    report(pages_fetched=0, papers_seen=0)

    result = runner.invoke(cli.app, ["ingest", "-c", "cs.Al"])

    assert result.exit_code == 1


def test_the_empty_feed_warning_names_the_category_and_the_taxonomy(report):
    """Asserted on stderr, which is where loguru actually writes — so this checks what
    the operator sees, not what a test-only sink was handed."""
    report(pages_fetched=0, papers_seen=0)

    result = runner.invoke(cli.app, ["ingest", "-c", "cs.Al", "-c", "cs.Xy"])

    assert "WARNING" in result.stderr
    assert "cs.Al, cs.Xy" in result.stderr
    assert "arxiv.org/category_taxonomy" in result.stderr


def test_a_run_that_fetched_pages_exits_zero(report):
    """The guard must not fire on a quiet incremental run: nothing *new* is not nothing
    *fetched*, and a re-run still pulls full pages of unchanged papers."""
    report(pages_fetched=4, papers_seen=800, unchanged=800, stopped_early=True)

    result = runner.invoke(cli.app, ["ingest", "-c", "cs.AI"])

    assert result.exit_code == 0
    assert "800 unchanged" in result.stderr


def test_no_category_explains_what_to_type(report):
    """Typer's stock "Missing option" names what is absent, not what to do about it —
    and this is the one flag with no sane default."""
    result = runner.invoke(cli.app, ["ingest"])

    assert result.exit_code == 2
    assert "--category / -c is required" in result.stderr
    assert "paper ingest -c cs.AI -c cs.CL" in result.stderr  # a copyable example
    assert "arxiv.org/category_taxonomy" in result.stderr  # where codes come from
    assert "all categories" in result.stderr  # why there is no default
