"""tools4ynab budget snapshot / compare, end to end against the fake YNAB."""

import json
from datetime import date, datetime

from cli_helpers import invoke, tools4ynab

from tools4ynab.budget.cli import budget
from tools4ynab.budget.snapshot import Comparison, Row

NOV = "2026-11-01"
MONTH = ["--month", "2026-11"]


def main(args):
    return invoke(budget, args, "tools4ynab budget")


def seed(y):
    y.groups[0]["categories"].append({**y._cat("Retired"), "hidden": True})
    y.groups.append(
        {"name": "Internal Master Category", "deleted": False, "categories": [y._cat("Inflow")]}
    )
    y.groups.append(
        {"name": "Credit Card Payments", "deleted": False, "categories": [y._cat("Visa")]}
    )
    for name, dollars in (
        ("Groceries", 600),
        ("Restaurants", 150),
        ("Comedy", 5),
        ("Assistant", 20),
        ("Visa", 100),
        ("Inflow", 1),
    ):
        y.assigned[(NOV, y.cat_id(name))] = dollars * 1000
    y.to_be_budgeted[NOV] = 0


def set_month(y, assigned=None, spent=None):
    for name, dollars in (assigned or {}).items():
        y.assigned[(NOV, y.cat_id(name))] = round(dollars * 1000)
    for name, dollars in (spent or {}).items():
        y.activity[(NOV, y.cat_id(name))] = -round(dollars * 1000)


def test_snapshot_saves_the_month_and_writes_nothing_to_ynab(ynab, tmp_path, capsys):
    seed(ynab)
    assert main(["snapshot", *MONTH]) == 0
    out = capsys.readouterr().out
    assert "Saved the snapshot of November 2026" in out and "snapshots/2026-11.json" in out
    assert "Assigned: 875.00" in out

    snap = json.loads((tmp_path / "snapshots" / "2026-11.json").read_text())
    assert snap["month"] == "2026-11" and snap["version"] == 1
    datetime.fromisoformat(snap["taken_at"])
    by_name = {c["name"]: c for c in snap["categories"]}
    assert by_name["Groceries"] == {
        "id": "cat-groceries",
        "group": "Everyday",
        "name": "Groceries",
        "assigned": 600_000,
        "activity": 0,
        "available": 600_000,
    }
    assert "Sketches" in by_name  # empty but visible: kept, so a later top-up shows
    assert "Retired" not in by_name and "Inflow" not in by_name  # hidden+empty, internal
    assert all(method == "GET" for method, _ in ynab.calls)


def test_snapshot_mentions_unassigned_money(ynab, capsys):
    seed(ynab)
    ynab.to_be_budgeted[NOV] = 250_000
    assert main(["snapshot", *MONTH]) == 0
    assert "250.00 is still in Ready to Assign" in capsys.readouterr().out


def test_snapshot_is_never_replaced_without_force(ynab, tmp_path, capsys):
    seed(ynab)
    assert main(["snapshot", *MONTH]) == 0
    path = tmp_path / "snapshots" / "2026-11.json"
    first = path.read_text()
    set_month(ynab, {"Groceries": 900})

    assert main(["snapshot", *MONTH]) == 2
    assert "already exists" in capsys.readouterr().err
    assert path.read_text() == first

    assert main(["snapshot", *MONTH, "--force"]) == 0
    assert '"assigned": 900000' in path.read_text()


def test_compare_shows_moves_new_money_and_overspending(ynab, capsys):
    seed(ynab)
    assert main(["snapshot", *MONTH]) == 0
    capsys.readouterr()

    set_month(
        ynab,
        assigned={"Groceries": 750, "Restaurants": 100, "Sketches": 200, "Visa": 400},
        spent={
            "Groceries": 742.10,
            "Restaurants": 80,
            "Comedy": 5,
            "Assistant": 25,
            "Visa": 400,
        },
    )
    assert main(["compare", *MONTH]) == 0
    out = capsys.readouterr().out

    assert "4 categories changed after the snapshot, 2 spent more than planned" in out
    assert "Assigned: 875.00 at the snapshot, 1,475.00 now (+600.00)" in out
    assert "Moved between categories: 50.00" in out  # Restaurants -50 went elsewhere
    assert "New money assigned:       600.00" in out
    # Card payments are not spending: Visa is in neither the total nor "spent more".
    assert "Spent: 852.10 of 775.00 planned" in out or "Spent so far: 852.10" in out

    changed = out.split("Changed after the snapshot:")[1].split("Spent more")[0]
    order = [n for n in ("Visa", "Sketches", "Groceries", "Restaurants") if n in changed]
    assert order == ["Visa", "Sketches", "Groceries", "Restaurants"]  # top-ups, then cuts
    assert "+150.00" in changed and "-50.00" in changed and "Comedy" not in changed

    over = out.split("Spent more than the snapshot planned:")[1]
    assert over.index("Groceries") < over.index("Assistant")
    assert "142.10" in over and "5.00" in over and "Visa" not in over
    assert "1 category spent less than planned (70.00 in total)." in out  # Restaurants


def test_compare_handles_categories_deleted_since_the_snapshot(ynab, capsys):
    seed(ynab)
    assert main(["snapshot", *MONTH]) == 0
    capsys.readouterr()
    ynab.groups[0]["categories"][1]["deleted"] = True  # Comedy
    assert main(["compare", *MONTH]) == 0
    out = capsys.readouterr().out
    assert "Everyday: Comedy (deleted)" in out and "-5.00" in out
    assert "Back to Ready to Assign:  5.00" in out
    # Nothing spent anywhere: everything but the deleted Comedy is under plan.
    assert "3 categories spent less than planned (770.00 in total)." in out


def test_compare_with_nothing_changed(ynab, capsys):
    seed(ynab)
    assert main(["snapshot", *MONTH]) == 0
    assert main(["compare", *MONTH]) == 0
    assert "Nothing changed after the snapshot." in capsys.readouterr().out


def test_compare_without_a_snapshot_lists_the_ones_there_are(ynab, capsys):
    seed(ynab)
    assert main(["snapshot", *MONTH]) == 0
    assert main(["compare", "--month", "2026-12"]) == 2
    err = capsys.readouterr().err
    assert "no snapshot of December 2026" in err and "2026-11 (pick one with --month)" in err


def test_quiet_mode_prints_counts_only(ynab, capsys, monkeypatch):
    seed(ynab)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert main(["snapshot", *MONTH]) == 0
    set_month(ynab, {"Groceries": 750}, {"Groceries": 742.10})
    assert main(["compare", *MONTH]) == 0
    out = capsys.readouterr().out
    assert "1 category changed after the snapshot, 1 spent more than planned" in out
    assert "Groceries" not in out and "600" not in out and "742" not in out


def test_money_handed_back_is_not_counted_as_moved():
    rows = [Row("A: One", 100_000, 60_000, 0), Row("A: Two", 50_000, 50_000, 0)]
    c = Comparison(date(2026, 11, 1), datetime(2026, 11, 1), rows)
    assert (c.added, c.removed, c.moved, c.net) == (0, 40_000, 0, -40_000)


def test_reachable_from_the_top_level_command(ynab, capsys):
    seed(ynab)
    assert tools4ynab(["budget", "snapshot", *MONTH]) == 0
    assert tools4ynab(["budget", "compare", *MONTH]) == 0
    assert "compared with the snapshot from" in capsys.readouterr().out
