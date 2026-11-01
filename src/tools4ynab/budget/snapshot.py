"""Lock in a month's budget as a snapshot, and later compare the month with it.

YNAB's month endpoint only has the *current* assigned amounts, with no history, so the
amounts at the moment the budget is locked in exist only if we save them. The month-end
side never needs saving: a month's numbers can be fetched any time.

All amounts are YNAB milliunits. Categories are matched by id, so renames don't matter."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .plan import INTERNAL_GROUP

VERSION = 1
# Activity in these is card payments, not spending, so it has no "plan vs spent".
CREDIT_CARD_GROUP = "Credit Card Payments"


def money(milli: int) -> str:
    return f"{milli / 1000:,.2f}"


def signed(milli: int) -> str:
    return f"{'+' if milli > 0 else '-' if milli < 0 else ''}{abs(milli) / 1000:,.2f}"


def take_snapshot(
    month: date, categories: list[dict[str, Any]], month_data: dict[str, Any], taken_at: datetime
) -> dict[str, Any]:
    """The JSON-ready snapshot of `month`: every category's assigned, activity and available.

    Ready to Assign is left out (it's the month-level `to_be_budgeted`), and so are hidden
    categories with nothing in them."""
    groups = {c["id"]: c["group_name"] for c in categories}
    rows = []
    for cat in month_data["categories"]:
        group = groups.get(cat["id"])
        if cat["deleted"] or group is None or group == INTERNAL_GROUP:
            continue
        values = cat["budgeted"], cat["activity"], cat["balance"]
        if cat["hidden"] and not any(values):
            continue
        rows.append(
            {
                "id": cat["id"],
                "group": group,
                "name": cat["name"],
                "assigned": values[0],
                "activity": values[1],
                "available": values[2],
            }
        )
    return {
        "version": VERSION,
        "month": f"{month:%Y-%m}",
        "taken_at": taken_at.isoformat(timespec="seconds"),
        "units": "milliunits",
        "income": month_data.get("income", 0),
        "to_be_budgeted": month_data.get("to_be_budgeted", 0),
        "categories": rows,
    }


def total_assigned(snapshot: dict[str, Any]) -> int:
    return sum(c["assigned"] for c in snapshot["categories"])


@dataclass(frozen=True)
class Row:
    label: str  # "Group: Category"
    locked: int  # assigned in the snapshot
    final: int  # assigned now
    spent: int  # -activity: positive when money went out
    # False for card payment categories (their activity is payments, not spending) and
    # deleted ones, which are left out of plan vs spent.
    tracks_spending: bool = True

    @property
    def change(self) -> int:
        return self.final - self.locked

    @property
    def variance(self) -> int:
        """Planned minus spent: negative means more was spent than the snapshot planned."""
        return self.locked - self.spent


@dataclass
class Comparison:
    month: date
    taken_at: datetime
    rows: list[Row] = field(default_factory=list)

    @property
    def changed(self) -> list[Row]:
        """Categories whose assigned amount changed, biggest top-ups first, then the cuts."""
        rows = [r for r in self.rows if r.change]
        return sorted(rows, key=lambda r: (r.change < 0, -abs(r.change), r.label))

    @property
    def over(self) -> list[Row]:
        """Categories that spent more than the snapshot planned, most over first."""
        rows = [r for r in self.rows if r.tracks_spending and r.variance < 0]
        return sorted(rows, key=lambda r: (r.variance, r.label))

    @property
    def under(self) -> list[Row]:
        return [r for r in self.rows if r.tracks_spending and r.variance > 0]

    @property
    def added(self) -> int:
        return sum(r.change for r in self.rows if r.change > 0)

    @property
    def removed(self) -> int:
        return -sum(r.change for r in self.rows if r.change < 0)

    @property
    def moved(self) -> int:
        """Money shifted between categories: what came out of some and went into others."""
        return min(self.added, self.removed)

    @property
    def net(self) -> int:
        """Positive: new money assigned since the snapshot (e.g. income that arrived later).
        Negative: money taken back to Ready to Assign."""
        return self.added - self.removed

    def totals(self) -> tuple[int, int, int]:
        """Assigned at the snapshot, assigned now, and planned spending (not card payments)."""
        return (
            sum(r.locked for r in self.rows),
            sum(r.final for r in self.rows),
            sum(r.spent for r in self.rows if r.tracks_spending),
        )

    def summary(self) -> str:
        """Counts only: safe for logs anyone with repo access can read."""
        return (
            f"{len(self.changed)} categor{'y' if len(self.changed) == 1 else 'ies'} changed"
            f" after the snapshot, {len(self.over)} spent more than planned"
        )

    def lines(self, in_progress: bool) -> list[str]:
        locked, final, spent = self.totals()
        planned = sum(r.locked for r in self.rows if r.tracks_spending)
        out = [
            f"Assigned: {money(locked)} at the snapshot, {money(final)} now"
            f" ({signed(final - locked)})"
        ]
        if self.moved:
            out.append(f"  Moved between categories: {money(self.moved)}")
        if self.net > 0:
            out.append(f"  New money assigned:       {money(self.net)}")
        elif self.net < 0:
            out.append(f"  Back to Ready to Assign:  {money(-self.net)}")
        out.append(
            f"Spent{' so far' if in_progress else ''}: {money(spent)} of {money(planned)} planned"
        )

        width = max((len(r.label) for r in self.changed + self.over), default=0)
        if self.changed:
            out += [
                "",
                "Changed after the snapshot:",
                f"  {'':<{width}} {'Snapshot':>11} {'Now':>11} {'Change':>11} {'Spent':>11}",
            ]
            for r in self.changed:
                spent = money(r.spent) if r.tracks_spending else ""
                out.append(
                    f"  {r.label:<{width}} {money(r.locked):>11} {money(r.final):>11}"
                    f" {signed(r.change):>11} {spent:>11}"
                )
        if self.over:
            out += [
                "",
                "Spent more than the snapshot planned:",
                f"  {'':<{width}} {'Planned':>11} {'Spent':>11} {'Over by':>11}",
            ]
            for r in self.over:
                out.append(
                    f"  {r.label:<{width}} {money(r.locked):>11} {money(r.spent):>11}"
                    f" {money(-r.variance):>11}"
                )
        if self.under:
            left = sum(r.variance for r in self.under)
            out += [
                "",
                f"{len(self.under)} categor{'y' if len(self.under) == 1 else 'ies'} spent less"
                f" than planned ({money(left)} in total).",
            ]
        if not self.changed and not self.over:
            out += ["", "Nothing changed after the snapshot."]
        return out


def compare(
    snapshot: dict[str, Any], categories: list[dict[str, Any]], month_data: dict[str, Any]
) -> Comparison:
    """The snapshot against the month as it is now, category by category."""
    if snapshot.get("version") != VERSION:
        raise ValueError(f"unsupported snapshot version {snapshot.get('version')!r}")
    month = date.fromisoformat(f"{snapshot['month']}-01")
    result = Comparison(month, datetime.fromisoformat(snapshot["taken_at"]))
    locked = {c["id"]: c for c in snapshot["categories"]}
    groups = {c["id"]: c["group_name"] for c in categories}

    for cat in month_data["categories"]:
        group = groups.get(cat["id"])
        was = locked.pop(cat["id"], None)
        if cat["deleted"] or group is None or group == INTERNAL_GROUP:
            if was:  # deleted since the snapshot: handled below
                locked[cat["id"]] = was
            continue
        if was is None and not (cat["budgeted"] or cat["activity"]):
            continue  # nothing then, nothing now
        result.rows.append(
            Row(
                f"{group}: {cat['name']}",
                was["assigned"] if was else 0,
                cat["budgeted"],
                -cat["activity"],
                group != CREDIT_CARD_GROUP,
            )
        )
    for was in locked.values():  # in the snapshot but gone now
        result.rows.append(
            Row(
                f"{was['group']}: {was['name']} (deleted)",
                was["assigned"],
                0,
                0,
                tracks_spending=False,
            )
        )
    return result
