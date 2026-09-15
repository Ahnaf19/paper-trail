"""The `ingest` and `wipe` commands."""

from typing import Annotated

import typer
from loguru import logger
from sqlalchemy import text

from app.core.errors import ArxivUnavailableError
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.models import Base
from app.services.ingest import run_ingest

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """Ingest arXiv papers into Postgres."""
    configure_logging()


@app.command()
def ingest(
    category: Annotated[
        list[str],
        typer.Option(
            "--category",
            "-c",
            help="arXiv category code, e.g. cs.AI. Repeatable. "
            "Codes: https://arxiv.org/category_taxonomy",
        ),
    ],
    max_papers: Annotated[int, typer.Option("--max", help="Stop after this many papers.")] = 1100,
    full: Annotated[
        bool, typer.Option("--full", help="Disable the early stop and re-scan everything.")
    ] = False,
) -> None:
    """Fetch papers from arXiv. Safe to re-run: existing rows are refreshed, not duplicated."""
    try:
        report = run_ingest(category, max_papers=max_papers, full=full)
    except ArxivUnavailableError as exc:
        # The database is consistent — every completed page committed. Re-running resumes.
        logger.error("arxiv unavailable: {}", exc)
        raise typer.Exit(code=1) from exc

    if report.pages_fetched == 0:
        # arXiv answers a mistyped category exactly as it answers a real empty one: 200,
        # totalResults=0, no entries. The cause is not knowable here, so name the likely
        # one and make the outcome loud rather than classify it wrongly.
        logger.warning(
            "arxiv returned no papers for {} — check the category codes against "
            "https://arxiv.org/category_taxonomy",
            ", ".join(category),
        )
        raise typer.Exit(code=1)

    logger.success(
        "done: {} pages, {} papers ({} new, {} updated, {} unchanged, {} embedded){}",
        report.pages_fetched,
        report.papers_seen,
        report.inserted,
        report.updated,
        report.unchanged,
        report.embedded,
        " [stopped early]" if report.stopped_early else "",
    )


@app.command()
def wipe(
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Delete every paper, author, category, link and embedding."""
    # Derived from the models, so a table added later cannot be forgotten here --
    # which is exactly the bug FR-1.8 calls out.
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    if not yes:
        typer.confirm(f"Delete all data from: {tables}?", abort=True)

    with SessionLocal() as session:
        session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        session.commit()
    logger.success("wiped: {}", tables)


if __name__ == "__main__":
    app()
