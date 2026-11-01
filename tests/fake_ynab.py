"""In-memory stand-in for the parts of the YNAB API this project uses.

`transfer_mode` models the one behavior YNAB's docs leave open: when an existing
transaction's payee is set to a transfer payee, either YNAB creates a new
counterpart ("create") or links an existing matching transaction ("link")."""

from __future__ import annotations

import itertools
import re
from copy import deepcopy
from datetime import date
from typing import Any

BUDGET = "budget-abc"


class FakeResponse:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status_code, self._body = status, body
        self.ok = status < 400
        self.text = str(body)

    def json(self) -> dict[str, Any]:
        return self._body


class FakeYnab:
    def __init__(self, transfer_mode: str = "create") -> None:
        self.transfer_mode = transfer_mode
        self._ids = itertools.count(1)
        self.fail_on: set[str] = set()  # "METHOD path-regex" that should return a 500
        self.calls: list[tuple[str, str]] = []
        self.groups = [
            {
                "name": "Everyday",
                "deleted": False,
                "categories": [
                    self._cat("Groceries"),
                    self._cat("Comedy"),
                    self._cat("Assistant"),
                    self._cat("Sketches"),
                    self._cat("Restaurants"),
                ],
            }
        ]
        self.accounts = {
            "chk": {
                "id": "chk",
                "name": "Acme Checking",
                "transfer_payee_id": "xfer-chk",
                "deleted": False,
            },
            "sav": {
                "id": "sav",
                "name": "Acme Savings",
                "transfer_payee_id": "xfer-sav",
                "deleted": False,
            },
        }
        self.txns: dict[str, dict[str, Any]] = {}
        self.assigned: dict[tuple[str, str], int] = {}  # (YYYY-MM-01, category id) -> milliunits
        self.activity: dict[tuple[str, str], int] = {}  # same keys; negative is spending
        self.to_be_budgeted: dict[str, int] = {}  # YYYY-MM-01 -> milliunits

    @staticmethod
    def _cat(name: str) -> dict[str, Any]:
        return {"id": f"cat-{name.lower()}", "name": name, "deleted": False, "hidden": False}

    def cat_id(self, name: str) -> str:
        return f"cat-{name.lower()}"

    def cat_name(self, cat_id: str | None) -> str | None:
        return cat_id.removeprefix("cat-").title() if cat_id else None

    # -- building data -----------------------------------------------------
    def add(
        self, account: str, amount_dollars: float, payee: str, *, days_ago: int = 1, **kw: Any
    ) -> dict[str, Any]:
        from datetime import timedelta

        tid = f"txn-{next(self._ids)}"
        txn = {
            "id": tid,
            "date": (date.today() - timedelta(days=days_ago)).isoformat(),
            "amount": round(amount_dollars * 1000),
            "memo": None,
            "cleared": "cleared",
            "approved": False,
            "flag_color": None,
            "account_id": account,
            "account_name": self.accounts[account]["name"],
            "payee_id": None,
            "payee_name": payee,
            "import_payee_name": payee,
            "import_id": f"imp-{tid}",
            "category_id": None,
            "category_name": "Uncategorized",
            "transfer_account_id": None,
            "transfer_transaction_id": None,
            "subtransactions": [],
            "deleted": False,
        }
        txn.update(kw)
        self.txns[tid] = txn
        return txn

    def live(self) -> list[dict[str, Any]]:
        return [t for t in self.txns.values() if not t["deleted"]]

    # -- the HTTP surface ----------------------------------------------------
    def request(
        self, method: str, url: str, params: dict | None = None, json: dict | None = None, **_: Any
    ) -> FakeResponse:
        path = url.split("/v1", 1)[1]
        self.calls.append((method, path))
        for rule in self.fail_on:
            m, pattern = rule.split(" ", 1)
            if m == method and re.search(pattern, path):
                return FakeResponse(
                    500, {"error": {"detail": "boom: token test-key-123 rejected for budget-abc"}}
                )
        assert path.startswith(f"/budgets/{BUDGET}/"), path
        path = path.removeprefix(f"/budgets/{BUDGET}")

        if method == "GET" and path == "/categories":
            return self._ok({"category_groups": self.groups})
        if method == "GET" and path == "/accounts":
            return self._ok({"accounts": list(self.accounts.values())})
        if method == "GET" and path == "/transactions":
            since = (params or {}).get("since_date", "0000")
            return self._ok(
                {"transactions": [deepcopy(t) for t in self.txns.values() if t["date"] >= since]}
            )
        if method == "GET" and (m := re.fullmatch(r"/months/(\d{4}-\d{2}-01)", path)):
            cats = [c for g in self.groups for c in g["categories"]]
            return self._ok(
                {
                    "month": {
                        "month": m.group(1),
                        "income": 0,
                        "to_be_budgeted": self.to_be_budgeted.get(m.group(1), 0),
                        "categories": [self._month_category(m.group(1), c) for c in cats],
                    }
                }
            )
        if method == "PATCH" and (
            m := re.fullmatch(r"/months/(\d{4}-\d{2}-01)/categories/([\w-]+)", path)
        ):
            self.assigned[m.groups()] = json["category"]["budgeted"]
            return self._ok({"category": {"id": m.group(2)}})
        if m := re.fullmatch(r"/transactions/([\w-]+)", path):
            tid = m.group(1)
            if tid not in self.txns:
                return FakeResponse(404, {"error": {"detail": "not found"}})
            if method == "GET":
                return self._ok({"transaction": deepcopy(self.txns[tid])})
            if method == "PUT":
                self._update(self.txns[tid], json["transaction"])
                return self._ok({"transaction": deepcopy(self.txns[tid])})
            if method == "DELETE":
                self.txns[tid]["deleted"] = True
                return self._ok({"transaction": deepcopy(self.txns[tid])})
        if method == "PATCH" and path == "/transactions":
            for patch in json["transactions"]:
                self._update(self.txns[patch["id"]], patch)
            return self._ok({"transactions": []})
        raise AssertionError(f"unhandled fake request: {method} {path}")

    def _month_category(self, month: str, cat: dict[str, Any]) -> dict[str, Any]:
        budgeted = self.assigned.get((month, cat["id"]), 0)
        activity = self.activity.get((month, cat["id"]), 0)
        # No carryover from earlier months: enough for what the tools read.
        return {**cat, "budgeted": budgeted, "activity": activity, "balance": budgeted + activity}

    @staticmethod
    def _ok(data: dict[str, Any]) -> FakeResponse:
        return FakeResponse(200, {"data": data})

    def _update(self, txn: dict[str, Any], patch: dict[str, Any]) -> None:
        for key, value in patch.items():
            if key == "id":
                continue
            if key == "payee_id" and str(value).startswith("xfer-"):
                self._make_transfer(txn, value)
            elif key == "subtransactions":
                txn["subtransactions"] = [
                    {
                        "id": f"sub-{next(self._ids)}",
                        "amount": s["amount"],
                        "category_id": s["category_id"],
                        "deleted": False,
                    }
                    for s in value
                ]
            elif key == "category_id":
                txn["category_id"], txn["category_name"] = (
                    value,
                    self.cat_name(value) or "Uncategorized",
                )
            elif key == "payee_name":
                txn["payee_name"] = value
            else:
                txn[key] = value

    def _make_transfer(self, txn: dict[str, Any], payee_id: str) -> None:
        target = next(a["id"] for a in self.accounts.values() if a["transfer_payee_id"] == payee_id)
        counterpart = None
        if self.transfer_mode == "link":
            counterpart = next(
                (
                    t
                    for t in self.live()
                    if t["account_id"] == target
                    and t["amount"] == -txn["amount"]
                    and t["id"] != txn["id"]
                ),
                None,
            )
        if counterpart is None:
            counterpart = self.add(
                target,
                -txn["amount"] / 1000,
                self.accounts[txn["account_id"]]["name"],
                cleared="uncleared",
                import_id=None,
                import_payee_name=None,
            )
            counterpart["date"] = txn["date"]
        txn.update(
            transfer_account_id=target,
            transfer_transaction_id=counterpart["id"],
            payee_id=payee_id,
            payee_name=f"Transfer : {self.accounts[target]['name']}",
            category_id=None,
            category_name="Uncategorized",
        )
        counterpart.update(
            transfer_account_id=txn["account_id"],
            transfer_transaction_id=txn["id"],
            category_id=None,
            category_name="Uncategorized",
        )
