"""buy-it-twice CLI.

    python3 -m buyittwice init                                   write an example buyittwice.json
    python3 -m buyittwice ask -r agent-research --amount 12 --what "API credits" [--once|--monthly] [--dry] [--json]
                                                                 GO (exit 0, recorded unless --dry) or NO (exit 1)
    python3 -m buyittwice void ID --why "checkout failed"         give a GO's money back to the pool
    python3 -m buyittwice record 30 --what "groceries"            spend made outside the gate
    python3 -m buyittwice when 40                                 first date a 40 buy would clear
    python3 -m buyittwice status [--json]

Global flags: --config PATH (env BUYITTWICE_CONFIG, default ./buyittwice.json),
              --ledger PATH (env BUYITTWICE_LEDGER, default ./state/ledger.jsonl), --day YYYY-MM-DD.
Exit codes: 0 GO / ok, 1 NO, 2 usage or config error.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .core import ConfigError, Gate, default_config_path, money

EXAMPLE = {
    "daily_allowance": 20,
    "start": "2026-01-01",
    "opening_balance": 0,
    "keep_day_allowance": False,
    "currency": "$",
    "requesters": {
        "operator": {"daily_cap": 100},
        "agent-research": {"daily_cap": 10},
        "agent-ops": {"daily_cap": 5},
    },
    "cash": {
        "balance": 1500,
        "as_of": "2026-01-01",
        "emergency_reserve": 400,
        "paycheck": 0,
        "paydays": {"anchor": "2026-01-09", "every_days": 14},
        "bills": [
            {"name": "rent", "amount": 700, "day_of_month": 1},
            {"name": "phone", "amount": 35, "day_of_month": 12},
        ],
    },
}


def _gate(o) -> Gate:
    g = Gate.from_paths(o.config, o.ledger)
    if o.day:
        g.today = lambda: o.day
    return g


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="buyittwice", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--ledger")
    ap.add_argument("--day", type=date.fromisoformat, help="pretend today is this date")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    a = sub.add_parser("ask")
    a.add_argument("-r", "--requester", required=True)
    a.add_argument("--amount", type=float, required=True)
    a.add_argument("--what", required=True)
    a.add_argument("--once", action="store_true", help="one-time buy: no daily cap, every other rule binds")
    a.add_argument("--monthly", action="store_true", help="recurring charge: --amount is the full first charge")
    a.add_argument("--dry", action="store_true")
    a.add_argument("--json", action="store_true")
    v = sub.add_parser("void")
    v.add_argument("id")
    v.add_argument("--why", required=True)
    r = sub.add_parser("record")
    r.add_argument("amount", type=float)
    r.add_argument("--what", required=True)
    w = sub.add_parser("when")
    w.add_argument("price", type=float)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    o = ap.parse_args(argv)

    if o.cmd == "init":
        p = Path(o.config) if o.config else default_config_path()
        if p.exists():
            print(f"{p} already exists; not touching it")
            return 2
        p.write_text(json.dumps(EXAMPLE, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {p}: edit daily_allowance, start, requesters, and cash (or delete the cash block)")
        return 0

    try:
        g = _gate(o)
        sym = g.cfg.currency
        if o.cmd == "ask":
            row = g.ask(o.requester, o.amount, o.what, once=o.once, monthly=o.monthly, dry=o.dry)
            if o.json:
                print(json.dumps(row))
            else:
                kind = "monthly, first charge in full" if o.monthly else "one-time" if o.once else "capped"
                tail = " (dry, nothing recorded)" if o.dry else f" [{row['id']}]"
                print(f"{row['verdict']}: {money(row['amount'], sym)} {kind} for {o.requester}, "
                      f"{row['what']}: {row['why']}{tail}")
                if row.get("reason") and "fits_on" in row:
                    print(f"  fits on {row['fits_on']} at zero other spend" if row["fits_on"]
                          else "  no date in the next year fits")
            return 0 if row["verdict"] == "GO" else 1
        if o.cmd == "void":
            print(json.dumps(g.void(o.id, o.why)))
            return 0
        if o.cmd == "record":
            print(json.dumps(g.record(o.amount, o.what)))
            return 0
        if o.cmd == "when":
            d = g.when(o.price)
            need = money(g.need_for(o.price), sym)
            if d:
                print(f"{money(o.price, sym)} clears on {d.isoformat()} ({d:%a}): spendable reaches {need}")
                return 0
            print(f"{money(o.price, sym)} does not clear in the next year (needs {need} spendable)")
            return 1
        st = g.status()
        if o.json:
            print(json.dumps(st, indent=1))
        else:
            print(f"{st['day']}  {st['state']}  spendable {money(st['spendable'], sym)}  "
                  f"(pool {money(st['pool'], sym)}, {money(st['rate'], sym)}/day)")
            if "cash" in st:
                c = st["cash"]
                print(f"  cash {money(c['cash'], sym)} - bills before {c['next_payday']} {money(c['must_pay'], sym)}"
                      f" - reserve {money(c['reserve'], sym)} = {money(c['after_bills'], sym)}")
            for n, v in st["requesters"].items():
                print(f"  {n:<20} {money(v['spent_today'], sym)} of {money(v['cap'], sym)} today")
        return 0
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
