# tools4ynab

Command-line tools to automate [YNAB](https://www.ynab.com) budgets, all run through one
command, `tools4ynab`, and sharing one API client and email setup. So far:

- **`tools4ynab rules`** auto-categorizes transactions with Monarch-style rules from a
  `rules.yaml` you write.
- **`tools4ynab reports`** is read-only: a monthly report on spending trends and recurring
  charges (see [Reports](#reports)).
- **`tools4ynab budget`** copies this month's assigned amounts into next month as a
  starting point, and snapshots a month's budget to compare with at month end (see
  [Budget](#budget)).

This repo is only the code. Each person keeps their own budget details (`rules.yaml`,
`reports.yaml`, `.env`) in a separate, private config repo or folder and installs
tools4ynab there. Nothing in this repo is specific to anyone's budget.

Requires Python 3.14 (uv installs it for you).

## Setting up your own config

Make a folder (ideally a private git repo) for your budget:

```
my-budget/
  pyproject.toml   # depends on tools4ynab
  rules.yaml       # your rules (see Rules below)
  reports.yaml     # optional report settings
  .env             # your API key and email settings; never commit this
  .gitignore       # .env, .venv/, audit/
```

`pyproject.toml`:

```toml
[project]
name = "my-budget"
version = "0"
requires-python = ">=3.14"
dependencies = ["tools4ynab"]

[tool.uv]
package = false

[tool.uv.sources]
# Pin a tag or commit so updates to the shared code only reach you when you choose.
tools4ynab = { git = "https://github.com/DasAmpharos/tools4ynab", tag = "v0.2.0" }
# Or, while working on the shared code next to it:
# tools4ynab = { path = "../tools4ynab", editable = true }
```

Then:

```bash
uv sync                          # installs Python + tools4ynab into .venv
cp ../tools4ynab/.env.example .env   # then fill in YNAB_API_KEY
uv run tools4ynab rules          # dry run: prints what would change
uv run tools4ynab rules --apply  # writes the changes to YNAB
uv run tools4ynab reports        # prints the monthly report
```

`tools4ynab rules` with no subcommand is `tools4ynab rules run`, and `tools4ynab reports`
is `tools4ynab reports monthly`. Options go after the command name
(`tools4ynab reports drift --email`), and `-h` works at every level. Commands read
`rules.yaml`, `reports.yaml` and `.env` from the current folder (`--rules` and
`--config` point elsewhere).

Each person uses their own personal access token (YNAB > Account Settings > Developer
Settings). YNAB's terms say tokens are for the account owner only, so don't share them.
`YNAB_BUDGET_ID` is optional (defaults to your last-used budget); it's the id in the
budget's URL.

## Rules commands

| Command | What it does |
|---|---|
| `run [--apply]` | Apply the rules. A dry run unless `--apply`. The default command. |
| `backtest [--months 6]` | Replay the rules over history and flag any transaction where a rule would have chosen differently from you (exit code 1 if any). Also checks splits and transfer pairing. |
| `unmatched [--months 6]` | Payees no rule covers yet, busiest first. |
| `suggest [--output FILE]` | Propose rules that were right every time in your history (see below). |
| `explain TEXT` | For transactions whose payee contains TEXT (or whose id starts with it), show which rule matches, what it would change, or why nothing happens. |
| `duplicates` | Manual, still-uncleared entries that look like a bank import YNAB failed to match, which double-counts money. |
| `undo [FILE] [--apply]` | Restore field edits from an audit log (default: the latest). |

All of these except `undo` are read-only. `run` also warns about possible duplicates.

## Rules

Rules run top to bottom and the **first match wins** per transaction. Within a rule,
every `match` key must hold; a list of values for one key means "any of".

| `match` key | Meaning |
|---|---|
| `payee_contains`, `payee_equals`, `payee_regex` | Payee name (case-insensitive) |
| `memo_contains`, `memo_regex` | Memo text |
| `account` | Account name(s) |
| `amount` | `{min, max, equals}` in dollars, absolute value |
| `direction` | `outflow` or `inflow` |

| `set` key | Effect |
|---|---|
| `category` | Category name, or `"Group: Category"` if the name is in several groups |
| `payee` | Rename the payee |
| `memo` | Set the memo |
| `flag_color` | `red/orange/yellow/green/blue/purple`, or `none` to clear |
| `approve` | `true` / `false`. Defaults to `true` whenever the rule sets a category |

### How existing categories are treated

YNAB auto-categorizes imported transactions with whatever category the payee last
had, and leaves them unapproved. So rules treat the two states differently:

- **Unapproved** transaction: its category is just YNAB's guess, so a matching rule
  replaces it (the output shows `was <old category>`).
- **Approved** transaction: the category is final and a rule never changes it,
  whether you or an earlier run set it. If you recategorize something a rule
  handled, it stays as you left it.

A rule that sets a category also approves the transaction (unless it says
`approve: false`), which is what locks the category in.

`settings.scope` picks which transactions are considered: `unapproved` (default;
unapproved ones plus anything still uncategorized), `uncategorized`, or `all`.
`settings.since_days` is how far back to look. Split transactions and transfers are
always skipped. Runs are idempotent: unchanged fields are not re-sent.

Typos in rule keys or category names fail the run before anything is written.

Rule names must be unique. Rules can also live in `rules.d/*.yaml` next to
`rules.yaml` (only `rules`, `splits` and `transfers` keys). They are consulted after
the main file, so hand-written rules always take priority; the weekly suggestion
pull request adds its rules there.

## Splits (bundled charges)

When one charge covers several categories in fixed amounts (e.g. an app store billing three
subscriptions at once), a `splits` rule divides it:

```yaml
splits:
  - name: AppHub bundle
    match: {payee_equals: AppHub, amount: {equals: 27.98}, direction: outflow}
    parts:
      - {category: Comedy, amount: 4.99}
      - {category: Assistant,  amount: 15.00}
      - {category: Sketches,   amount: 7.99}
```

`match` must give the exact total and a direction, and the parts must add up to it
(checked when the file loads). Because the total is exact, a price change makes the
rule stop matching and the transaction is left for you to split by hand. Splits
take priority over normal rules, approve the transaction, and follow the same
approved-is-final guarantee. After each split the tool confirms YNAB created the
subtransactions.

## Transfers (e.g. Acme Round Up)

Banks often import both halves of a transfer as separate transactions. A `transfers`
rule pairs an outflow in one account with the same-amount inflow in another (closest
date within `max_days_apart`) and merges them into a single YNAB transfer:

```yaml
transfers:
  - name: Acme Round Up
    from: {account: Acme Checking, payee_contains: round up transfer}
    to:   {account: Acme Savings,  payee_contains: roundup credit}
```

The API can't link two existing transactions, so the outflow is converted to a
transfer (which makes YNAB create the savings side) and the redundant imported
inflow is deleted. The delete only happens after YNAB confirms the new transfer,
and never if YNAB linked the existing pair instead. Only complete pairs are
touched: if one side hasn't arrived yet, nothing happens until it does.

Payee criteria match both the payee shown in YNAB and the name the bank originally
sent (`import_payee_name`), so renamed payees still match.

## Suggested rules

`suggest` only proposes a rule when every transaction for a payee (or a payee at one
exact amount) landed in a single category, at least 3 times, recently, for payees
nothing covers yet. It is deliberately conservative:

- Payees you have ever split never get an "any amount" rule (auto-approving would
  lock out the split you sometimes need).
- Closed-trip categories count against a payee being consistent, so restaurants you
  visit while travelling don't turn into rules.
- A single fixed price becomes an exact-amount rule rather than an any-amount one.

`suggest --output rules.d/suggested.yaml` writes the candidates where they are picked up
as rules. Delete lines to drop some, or add a payee to `settings.suggest_ignore` in
`rules.yaml` to stop it being suggested.

## Audit log and undo

Every applied change is appended to `audit/<run>.jsonl` (what changed, the old and
new values, which rule). Locally that is a folder you can ignore (it is gitignored).
If you run the tool in GitHub Actions, upload `audit/` as a run artifact named `audit`
(also when a run fails partway) and `undo --from-github` can fetch it back.

`undo` restores field edits (category, payee, memo, flag, approval), but only where
the field still holds the value the run set, so anything you changed afterwards is
left alone. Splits and merged transfers can't be undone automatically (YNAB's API
can't unsplit or recreate a deleted transaction); `undo` lists them so you can fix
them in YNAB. The email for each run includes the exact command to undo it.

```bash
uv run tools4ynab rules undo                       # preview undoing the newest local log
uv run tools4ynab rules undo --from-github         # ...or the latest Actions run (needs gh)
uv run tools4ynab rules undo --from-github 4242424 # a specific Actions run id
uv run tools4ynab rules undo --from-github --apply # actually restore
```

## Email notifications

Set these (in `.env`, or as environment variables) to get an email whenever a run
applies changes or fails. Nothing is sent for dry runs or runs that change nothing.
A failure to send never fails the run.

| Variable | Meaning |
|---|---|
| `SMTP_USER`, `SMTP_PASSWORD` | Login. For Gmail, create an [app password](https://myaccount.google.com/apppasswords) (needs 2-step verification). |
| `NOTIFY_EMAIL_TO` | Comma-separated recipients. |
| `SMTP_HOST`, `SMTP_PORT` | Default `smtp.gmail.com` and `587` (STARTTLS; port 465 uses SSL). |
| `NOTIFY_EMAIL_FROM` | Defaults to `SMTP_USER`. |

Check the settings any time with `uv run tools4ynab rules test-email`, which sends a sample
message (nothing is changed in YNAB) and tells you why if it can't.

The email lists each change, links to the GitHub Actions run when there is one, and never includes your API
key, budget id or SMTP password.

**Gmail setup:** turn on 2-Step Verification for the account, then create an app
password at <https://myaccount.google.com/apppasswords> (Google shows it as four
groups of four letters; the spaces are ignored). Use your Gmail address as
`SMTP_USER` and that app password as `SMTP_PASSWORD`. The default host and port
already target Gmail. An app password grants full access to the mailbox, so
consider a dedicated Gmail account just for sending and put your real addresses in
`NOTIFY_EMAIL_TO`.

## Reports

`tools4ynab reports` looks at your history and tells you what's *changing*, which YNAB's own
charts leave you to spot yourself. It never writes to YNAB.

```bash
uv run tools4ynab reports                    # print the monthly report
uv run tools4ynab reports drift              # just the spending trends
uv run tools4ynab reports subscriptions      # just the recurring charges
uv run tools4ynab reports --email            # also email it
uv run tools4ynab reports --as-of 2026-08-01 # as of another date
```

**What the email contains**, top to bottom:

- **At a glance:** three or four plain-English sentences (what's trending, what's down, the
  biggest one-off, and the recurring total).
- **Worth a look:** the only things that need action: a recurring charge that stopped
  (possibly a missed payment), a price change (naming any exact-amount rule it broke), and
  new recurring charges. Charges gone for over two months are grouped as "Looks cancelled".
- **Spending creeping up / that's come down:** the average of the last 3 complete months is
  compared with the 3 before (the current and any partial first month are skipped). A
  category appears only if it moved at least $25 a month *and* 30%. Each row shows the
  payees behind it and a 6-month bar chart (darker bars are the recent months).
- **One-time purchases:** categories where a rise is almost all in a single month. These are
  never presented as trends.
- **Recurring charges:** charges that repeat monthly at a fixed price, with the monthly
  total. Fixed charges are left out of trends, because a payment that lands a day early
  would otherwise look like a swing.

Each section is capped (6 rows by default) and says "+N more" for the rest. Splits count
under their own categories; transfers and income are ignored. Annual charges can't be
detected until there is more than a year of history.

To preview the email without sending it: `uv run tools4ynab reports --save-html report.html`,
then open the file in a browser.

**Settings** are optional, in `reports.yaml` in your config repo: which category groups to
exclude (by default any group whose name contains "completed", for closed trips), the
thresholds, and how many items to list. Payees and categories are exactly what YNAB has,
so renaming them in YNAB changes the report.

Report recipients are `REPORT_EMAIL_TO` if set, otherwise `NOTIFY_EMAIL_TO`, so you can
send reports to someone without sending them the rule notifications.

## Budget

`tools4ynab budget` (short for `tools4ynab budget copy`) fills in next month's budget with
what you assigned this month, so you start from last month's plan and adjust instead of
starting from zero. Like `rules`, it's a dry run unless `--apply`:

```bash
uv run tools4ynab budget                      # preview copying this month into next month
uv run tools4ynab budget --apply              # write it to YNAB
uv run tools4ynab budget --from 2026-10 --to 2027-01   # any two months
uv run tools4ynab budget --overwrite --apply  # also replace amounts already set
```

- Only categories with an amount assigned in the `--from` month are copied. Others are left
  as they are; nothing is set to zero.
- An amount you've already assigned in the `--to` month is kept (the preview lists it)
  unless you pass `--overwrite`.
- Hidden categories and Ready to Assign are skipped.
- It copies *assigned* amounts, not what you spent.

Each category is one API call, and YNAB allows 200 an hour. If a run stops partway, the
error says how far it got. Run it again to finish, because categories already copied are
skipped.

### Snapshots: compare the plan with the month end

YNAB only keeps a category's *current* assigned amount, so once you move money around
mid-month the original plan is gone. `budget snapshot` saves it when you've finished
budgeting, and `budget compare` shows what happened since:

```bash
uv run tools4ynab budget snapshot                  # lock in this month's budget
uv run tools4ynab budget snapshot --month 2026-11  # ...or another month (e.g. the day before)
uv run tools4ynab budget compare                   # this month vs its snapshot
uv run tools4ynab budget compare --month 2026-11   # any month with a snapshot
```

`compare` shows:

- **Assigned** at the snapshot and now, split into money **moved between categories**
  and **new money assigned** (e.g. income that arrived after you budgeted) or money taken
  **back to Ready to Assign**.
- **Changed after the snapshot:** each category you topped up or cut, with what it spent.
- **Spent more than the snapshot planned:** categories that overspent the original plan,
  even if you covered them by moving money in.

Both commands only read from YNAB. Snapshots are small JSON files in `snapshots/`
(`--dir` to change), one per month, holding each category's assigned, activity and
available amounts. Commit them to your private config repo to keep the history. A month's
snapshot is never replaced unless you pass `--force`.

Run `compare` on the last day of the month or any time after: YNAB's numbers for a past
month stay put, so the comparison is the same whenever you run it. Only the net change is
visible: moving money from A to B and back looks like no change. Credit card payment
categories show their assigned changes but aren't counted as spending.

## Logging and privacy

GitHub Actions logs, step summaries and artifacts are readable by anyone with access to the
repo, so in Actions (detected via `GITHUB_ACTIONS`) every command runs in **quiet mode**:

| In the Actions log | Never in the Actions log |
|---|---|
| Counts, rule names, and which fields a rule changed | Payees, amounts, categories, account names |
| One line per API call: method, path, status, seconds (budget id hidden) | The report text, the step summary details |
| On failure: the error, plus *where* in the code it happened | Exception text that could contain data |

The audit artifact is redacted the same way: no payee, amount or account, and a transfer
keeps only the id, date and cleared state of the transaction it removed. It keeps what `undo`
needs, including the old and new payee/memo when a rule renames one. **Emails are not
redacted**: they go to your inbox and carry the full details.

Local runs print everything. To see details in an Actions log for a one-off debugging run,
set `YNAB_VERBOSE=1`. To see quiet mode locally: `GITHUB_ACTIONS=true uv run tools4ynab rules`.

## Disabling rules

Set `YNAB_DISABLED_RULES` (in `.env` or the environment) to a comma-separated list of
rule names, exactly as they appear in `name:` in `rules.yaml`. `run` then skips those
rules (any kind: rule, split or transfer); the analysis commands still see them. Setting
it only where the tool runs on a schedule lets you test a rule by hand before enabling it
there. A name that doesn't match any rule fails the run before anything is written, so a
typo can't leave a rule running.

## Tests

```bash
uv run pytest            # unit tests plus end-to-end tests against a fake YNAB API
uv run ruff check && uv run ruff format --check
```

The end-to-end tests drive the whole program (`--apply`, audit log, email, undo,
failure handling) against an in-memory fake of the YNAB API, including both ways YNAB
might handle a transfer. Real YNAB behavior for splits and transfer merges has not
yet been confirmed against a live write; see `YNAB_DISABLED_RULES` above.

## Releasing

The version comes from the git tag; there is no version number to edit. Once the changes
are merged to `main`:

```bash
git switch main && git pull
git tag -a v0.3.0 -m "v0.3.0"   # semver: bump the minor for features, the patch for fixes
git push origin v0.3.0
```

Config repos then move to it by changing `tag = "v0.3.0"` and running `uv lock`.
`tools4ynab --version` shows what is installed. Between tags, builds get a development
version such as `0.3.1.dev2+g1a2b3c4`. Release tags can't be moved or deleted (a repo
rule), so a bad release is fixed with a new one.

## Disclaimer

We are not affiliated, associated, or in any way officially connected with YNAB or any of
its subsidiaries or affiliates. The names YNAB and You Need A Budget, and related names,
marks and images, are registered trademarks of YNAB.
