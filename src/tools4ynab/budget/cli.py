"""`tools4ynab budget`: set up a month's budget, and see how it changed.

`copy` (the default) fills next month with this month's assigned amounts as a starting
point; a dry run unless --apply. `snapshot` saves a month's assigned amounts once the
budget is locked in, and `compare` later shows what changed since then."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import click

from tools4ynab.core.client import YnabError
from tools4ynab.core.commands import DefaultGroup, execute
from tools4ynab.core.session import Session

from .plan import next_month, plan_copy
from .snapshot import compare, money, take_snapshot, total_assigned


def run_copy(
    session: Session,
    source: datetime | None,
    target: datetime | None,
    overwrite: bool,
    apply: bool,
) -> int:
    src = (source.date() if source else date.today()).replace(day=1)
    dst = target.date() if target else next_month(src)
    if dst == src:
        click.echo("error: --from and --to are the same month", err=True)
        return 2

    client = session.client
    plan = plan_copy(
        src, dst, client.categories(), client.assigned(src), client.assigned(dst), overwrite
    )
    mode = "Copying" if apply else "Dry run: copying"
    click.echo(f"{mode} assigned amounts from {src:%B %Y} to {dst:%B %Y}: {plan.summary()}")
    if not session.quiet:  # Actions logs are readable by anyone with repo access
        click.echo("\n".join(plan.lines()))

    if not apply:
        if plan.copy:
            click.echo("Re-run with --apply to write these to YNAB.")
        return 0

    for done, item in enumerate(plan.copy):
        try:
            client.set_assigned(dst, item.category_id, item.amount)
        except YnabError as e:
            # Categories already copied now match, so a re-run picks up where this stopped
            # (useful after YNAB's limit of 200 requests an hour).
            raise YnabError(
                f"{e} (after {done} of {len(plan.copy)}; re-run to copy the rest)"
            ) from None
    click.echo(f"Done: assigned {len(plan.copy)} categor{'y' if len(plan.copy) == 1 else 'ies'}.")
    return 0


def _month(value: datetime | None) -> date:
    return (value.date() if value else date.today()).replace(day=1)


def _snapshot_path(directory: Path, month: date) -> Path:
    return directory / f"{month:%Y-%m}.json"


def run_snapshot(session: Session, month: datetime | None, directory: Path, force: bool) -> int:
    target = _month(month)
    path = _snapshot_path(directory, target)
    if path.exists() and not force:
        click.echo(
            f"error: {path} already exists (the snapshot of {target:%B %Y} is locked in);"
            " pass --force to replace it",
            err=True,
        )
        return 2

    client = session.client
    snap = take_snapshot(
        target, client.categories(), client.month(target), datetime.now().astimezone()
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap, indent=2) + "\n")

    count = len(snap["categories"])
    click.echo(f"Saved the snapshot of {target:%B %Y} ({count} categories) to {path}.")
    if not session.quiet:  # Actions logs are readable by anyone with repo access
        click.echo(f"Assigned: {money(total_assigned(snap))}")
        if snap["to_be_budgeted"]:
            click.echo(
                f"Note: {money(snap['to_be_budgeted'])} is still in Ready to Assign. If you"
                " assign it later, `compare` will show it as new money."
            )
    return 0


def run_compare(session: Session, month: datetime | None, directory: Path) -> int:
    target = _month(month)
    path = _snapshot_path(directory, target)
    if not path.exists():
        found = sorted(p.stem for p in directory.glob("*.json"))
        hint = (
            f" Snapshots in {directory}: {', '.join(found)} (pick one with --month)."
            if found
            else ""
        )
        click.echo(f"error: no snapshot of {target:%B %Y} at {path}.{hint}", err=True)
        return 2

    snap = json.loads(path.read_text())
    client = session.client
    result = compare(snap, client.categories(), client.month(target))
    in_progress = _month(None) <= target
    status = " (month not over yet)" if in_progress else ""
    click.echo(
        f"{target:%B %Y} compared with the snapshot from"
        f" {result.taken_at:%b %-d, %Y at %-I:%M %p}{status}: {result.summary()}"
    )
    if not session.quiet:
        click.echo("\n".join(["", *result.lines(in_progress)]))
    return 0


month_type = click.DateTime(["%Y-%m"])
snapshot_dir = click.option(
    "--dir",
    "directory",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("snapshots"),
    show_default=True,
    help="Folder the snapshots are kept in (one YYYY-MM.json per month).",
)


@click.group(cls=DefaultGroup, default_command="copy")
def budget() -> None:
    """Set up a month's budget, and see how it changed.

    With no command, runs `copy`."""


@budget.command("copy")
@click.option(
    "--from",
    "source",
    type=month_type,
    metavar="YYYY-MM",
    help="Month to copy. [default: this month]",
)
@click.option(
    "--to",
    "target",
    type=month_type,
    metavar="YYYY-MM",
    help="Month to fill in. [default: the month after --from]",
)
@click.option(
    "--overwrite", is_flag=True, help="Also replace amounts already assigned in the --to month."
)
@click.option("--apply", is_flag=True, help="Write the amounts to YNAB.")
def cmd_copy(**params: Any) -> None:
    """Copy one month's assigned amounts into the next, as a starting point.

    A dry run unless --apply. Only categories with an amount assigned are copied, and
    amounts you already set in the --to month are kept unless --overwrite."""
    click.get_current_context().exit(execute(run_copy, params))


@budget.command("snapshot")
@click.option(
    "--month", type=month_type, metavar="YYYY-MM", help="Month to snapshot. [default: this month]"
)
@snapshot_dir
@click.option("--force", is_flag=True, help="Replace the month's existing snapshot.")
def cmd_snapshot(**params: Any) -> None:
    """Lock in a month's budget: save every category's assigned amount to a file.

    Run it once you've finished budgeting the month; `compare` later shows what changed
    since. Only reads from YNAB. An existing snapshot is never replaced without --force."""
    click.get_current_context().exit(execute(run_snapshot, params))


@budget.command("compare")
@click.option(
    "--month", type=month_type, metavar="YYYY-MM", help="Month to compare. [default: this month]"
)
@snapshot_dir
def cmd_compare(**params: Any) -> None:
    """Compare a month with its snapshot: what was moved, topped up, and overspent.

    Uses YNAB's current numbers for the month, so run it at the end of the month (or any
    time after) for the final picture. Only reads from YNAB."""
    click.get_current_context().exit(execute(run_compare, params, (YnabError, ValueError)))
