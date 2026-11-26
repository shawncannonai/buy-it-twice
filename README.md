# buy-it-twice

A spend gate for humans and AI agents: a purchase clears only when you could pay for it twice.

```
$ python3 -m buyittwice ask -r operator --amount 60 --what "monitor"
NO: $60.00 capped for operator, monitor: $60.00 needs $120.00 spendable; pool $92.00 [g592c551b22]
  fits on 2026-01-07 at zero other spend
```

## Why it exists

Agents and automations can now buy things: API credits, seats, domains, plan upgrades. The usual guardrail is
a monthly budget, and a budget fails the same way every time. Each purchase looks small and reasonable, each
one fits "the budget", and the account ends the month short because nobody checked the bills that land before
the next payday. Then a human taps "upgrade" on a card prompt and nothing checked that either.

This was learned the hard way. The fix that held is boring and strict:

- Money accrues at a **fixed daily allowance** into a pool. Unspent money rolls forward. Spending is limited by
  what has actually built up, not by a monthly number you hope holds.
- A purchase needs **twice its price** spendable. If you can only afford it once, you cannot afford it: the
  first surprise bill would put you under.
- If you tell it your cash, the pool is also capped by **cash minus the bills due before the next payday minus
  an emergency reserve**. A full pool does not help if rent is due Tuesday.
- **Every requester has a daily cap.** An agent stuck in a retry loop buys once, not forty times.
- There is **no override flag**.

## Install

Python 3.10+, standard library only.

```
pip install .            # or just put the buyittwice/ folder on your PYTHONPATH
python3 -m buyittwice init
```

`init` writes an example `buyittwice.json` in the current directory. State goes to `./state/ledger.jsonl`
(or `$XDG_STATE_HOME/buyittwice/ledger.jsonl` if `./state` does not exist and `XDG_STATE_HOME` is set).

## Quick example

The example config: $20/day from 2026-01-01, three requesters, a cash block. Real output, pretending today
is 2026-01-05 (`--day`):

```
$ python3 -m buyittwice --day 2026-01-05 status
2026-01-05  OPEN  spendable $100.00  (pool $100.00, $20.00/day)
  cash $800.00 - bills before 2026-01-09 $0.00 - reserve $400.00 = $400.00
  agent-ops            $0.00 of $5.00 today
  agent-research       $0.00 of $10.00 today
  operator             $0.00 of $100.00 today

$ python3 -m buyittwice --day 2026-01-05 ask -r agent-research --amount 8 --what "dataset download"
GO: $8.00 capped for agent-research, dataset download: spendable $100.00 -> $92.00 [g0fcdc667aa]

$ python3 -m buyittwice --day 2026-01-05 ask -r agent-research --amount 8 --what "second dataset"
NO: $8.00 capped for agent-research, second dataset: agent-research daily cap: $8.00 already today + $8.00 is over $10.00 [gd3b06ba47c]

$ python3 -m buyittwice --day 2026-01-05 when 60
$60.00 clears on 2026-01-07 (Wed): spendable reaches $120.00

$ python3 -m buyittwice --day 2026-01-05 ask -r operator --amount 15 --what "domain renewal" --monthly
GO: $15.00 monthly, first charge in full for operator, domain renewal: spendable $92.00 -> $77.00 [g0cb024ecc1]

$ python3 -m buyittwice --day 2026-01-05 ask -r stranger --amount 1 --what x
NO: $1.00 capped for stranger, x: unknown requester 'stranger': configured are agent-ops, agent-research, operator [g7a68603272]
```

Exit code is 0 on GO, 1 on NO, 2 on a usage or config error, so a shell script can gate on it:

```
python3 -m buyittwice ask -r agent-ops --amount 4.50 --what "cron host" && ./buy_cron_host.sh
```

## Python API (for agents)

```python
from buyittwice import require, void, check, SpendDenied

try:
    gid = require("agent-ops", 4.50, "one month of a hosted cron")   # GO: recorded as spend now
except SpendDenied as e:
    print("refused:", e.reason)       # e.g. "buy-it-twice", "over-cap", "deficit", "gate-error"
    raise

try:
    checkout()
except CheckoutFailed:
    void(gid, "checkout never charged")  # the money goes back to the pool (and the requester's cap)

ok, why = check("agent-ops", 4.50, "dry run")   # nothing recorded
```

`require` fails closed: a missing config, an unreadable ledger, or any exception inside the gate raises
`SpendDenied` with reason `gate-error`. An agent that cannot get a GO does not buy.

For tests or several budgets in one process, build a gate explicitly and pass it:

```python
from buyittwice import Gate
g = Gate.from_paths("team-budget.json", "state/team-ledger.jsonl")
require("agent-ops", 3, "x", gate=g)
```

## Commands

| command | does |
|---|---|
| `ask -r NAME --amount N --what TEXT` | GO (recorded) or NO with the reason. `--dry` records nothing, `--json` prints the row |
| `ask ... --once` | one-time buy: skips the requester's daily cap, every other rule binds |
| `ask ... --monthly` | recurring charge: `--amount` is the full first charge; skips the daily cap |
| `void ID --why TEXT` | a GO whose purchase never charged gives its money back |
| `record N --what TEXT` | spend made outside the gate (a human bought something), counts against the pool |
| `when PRICE` | first date a buy of PRICE would clear at zero other spend |
| `status [--json]` | pool, spendable, cash side, each requester's spend today |
| `init` | write an example config |

Global flags: `--config PATH` (env `BUYITTWICE_CONFIG`, default `./buyittwice.json`), `--ledger PATH`
(env `BUYITTWICE_LEDGER`), `--day YYYY-MM-DD` to evaluate as of another date.

## Config

```json
{
  "daily_allowance": 20,
  "start": "2026-01-01",
  "opening_balance": 0,
  "keep_day_allowance": false,
  "currency": "$",
  "requesters": {
    "operator":       {"daily_cap": 100},
    "agent-research": {"daily_cap": 10},
    "agent-ops":      {"daily_cap": 5}
  },
  "cash": {
    "balance": 1500,
    "as_of": "2026-01-01",
    "emergency_reserve": 400,
    "paycheck": 0,
    "paydays": {"anchor": "2026-01-09", "every_days": 14},
    "bills": [
      {"name": "rent",  "amount": 700, "day_of_month": 1},
      {"name": "phone", "amount": 35,  "day_of_month": 12},
      {"name": "insurance", "amount": 240, "due": "2026-03-15"}
    ]
  }
}
```

- `daily_allowance`, `start` (required): the pool gets `daily_allowance` every day from `start`, start day included.
- `opening_balance`: added once.
- `keep_day_allowance`: buy-it-twice needs `2 x price + daily_allowance`, so a purchase never eats the day.
- `requesters`: name to daily cap (a number or `{"daily_cap": N}`). A `"*"` entry covers every other name.
  Any name not listed (and no `"*"`) is refused.
- `cash` (optional, delete it to use the pool alone): `balance` is cash at the start of `as_of`. Bills
  are `day_of_month` (monthly, clamped to month end) or `due` (one date). `paydays` is a list of dates or
  `{"anchor", "every_days"}`. `paycheck` is income you are willing to assume lands on each payday (default 0,
  the conservative answer). Without paydays the must-pay window is `window_days` (default 30).

## How it works

Pool on day D = `opening_balance + daily_allowance x days(start..D) - live spends up to D`. Live spends are GO
rows not voided, plus RECORD rows. NO rows and `--dry` asks never count.

With a cash block, on day D: cash = `balance - gate spends since as_of - bills due before D + paycheck x
paydays since as_of`, and `after_bills = cash - bills due from D up to the next payday - emergency_reserve`.
Spendable = `min(pool, after_bills)`.

An ask is checked in this fixed order. The first rule that fails is the answer:

1. `bad-amount`: zero, negative, or not a number
2. `unknown-requester`: not in `requesters` and no `"*"`
3. `deficit`: cash after bills and reserve is below 0, or the pool is below 0
4. `used-up`: spendable is exactly 0
5. `over-cap`: the requester's spend today plus this would cross its daily cap (skipped for `--once` / `--monthly`)
6. `would-go-negative`: spendable minus this is below 0
7. `buy-it-twice`: spendable is below 2 x price (+ the day's allowance if `keep_day_allowance`)

The fixed order means the reason is stable and a script can branch on it: an agent seeing `over-cap` retries
tomorrow, one seeing `buy-it-twice` asks `when`.

`when PRICE` walks forward day by day (up to a year) with no further spend and returns the first day spendable
reaches the buy-it-twice need. A NO row carries the same date as `fits_on`.

The ledger is append-only JSONL. Nothing is rewritten; a void is a new row pointing at the GO. A line that is
not JSON stops the gate rather than being skipped, since a skipped line could be a spend.

## Why there is no override flag

Every gate with an override ends up as a gate that is always overridden. The first time it is "just this once,
it is important"; after that the flag is in a shell alias, and the agent that read your scripts has learned it
too. An override also makes the NO negotiable, so an agent's best move becomes arguing for it.

Without one, there are exactly two ways to buy something the gate refused:

1. Wait. `when` tells you the date, so waiting is a plan, not a vague no.
2. Change the config on purpose: raise the allowance, add a requester, raise a cap. That is a visible edit to a
   file you can keep in version control, made calmly, not a flag typed at checkout.

## Limits

- It is a ledger, not a bank feed. It knows what passed through it and what you `record`. Keep `cash.balance`
  and `as_of` current if you use the cash side.
- A `--monthly` ask counts the first charge only. Put renewals through `ask` again (or `record` them) each month.
- Single machine, single file. Two processes appending at once is fine for JSONL lines this short on a local
  disk; it is not a distributed lock.
- Dates are local calendar days. Use `--day` if your agents run across time zones and you want one clock.
- It cannot stop a purchase made by something that never calls it. Wire `require()` into every buy path.

## License

MIT
