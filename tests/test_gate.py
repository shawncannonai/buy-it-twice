import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path

from buyittwice import (BAD_AMOUNT, BUY_IT_TWICE, DEFICIT, GATE_ERROR, OVER_CAP, UNKNOWN_REQUESTER, USED_UP,
                        WOULD_GO_NEGATIVE, Config, Gate, Ledger, SpendDenied, require)
from buyittwice.__main__ import main
from buyittwice.core import _bill_dates, _paydays

D5 = date(2026, 1, 5)   # start 2026-01-01 at 10/day: pool 50 on this day with no spend


def make(tmp, **over):
    raw = {"daily_allowance": 10, "start": "2026-01-01",
           "requesters": {"agent-a": {"daily_cap": 10}, "agent-big": {"daily_cap": 1000}}}
    raw.update(over)
    return Gate(Config.from_dict(raw), Ledger(Path(tmp) / "state" / "ledger.jsonl"), today=lambda: D5)


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name

    def tearDown(self):
        self._td.cleanup()


class NoReasons(Base):
    def test_bad_amount(self):
        g = make(self.tmp)
        for amt in (0, -5, "abc"):
            self.assertEqual(g.ask("agent-a", amt, "x")["reason"], BAD_AMOUNT)

    def test_unknown_requester(self):
        g = make(self.tmp)
        r = g.ask("stranger", 1, "x")
        self.assertEqual((r["verdict"], r["reason"]), ("NO", UNKNOWN_REQUESTER))

    def test_wildcard_requester_allowed(self):
        g = make(self.tmp, requesters={"*": {"daily_cap": 5}})
        self.assertEqual(g.ask("anyone", 5, "x")["verdict"], "GO")

    def test_deficit_when_pool_negative(self):
        g = make(self.tmp)
        g.record(80, "outside spend", day=D5)   # pool 50 - 80 = -30
        r = g.ask("agent-a", 1, "x")
        self.assertEqual(r["reason"], DEFICIT)
        self.assertIn("-$30.00", r["why"])

    def test_deficit_when_cash_short(self):
        cash = {"balance": 100, "as_of": "2026-01-05", "emergency_reserve": 50,
                "paydays": ["2026-01-20"], "bills": [{"name": "rent", "amount": 100, "due": "2026-01-10"}]}
        g = make(self.tmp, cash=cash)   # 100 - 100 - 50 = -50
        r = g.ask("agent-a", 1, "x")
        self.assertEqual(r["reason"], DEFICIT)

    def test_used_up(self):
        g = make(self.tmp)
        g.record(50, "outside spend", day=D5)
        self.assertEqual(g.ask("agent-a", 1, "x")["reason"], USED_UP)

    def test_over_cap(self):
        g = make(self.tmp)
        self.assertEqual(g.ask("agent-a", 11, "x")["reason"], OVER_CAP)
        self.assertEqual(g.ask("agent-a", 6, "x")["verdict"], "GO")     # pool 50 >= 12
        r = g.ask("agent-a", 5, "x")                                    # 6 + 5 > 10
        self.assertEqual(r["reason"], OVER_CAP)

    def test_cap_resets_next_day(self):
        g = make(self.tmp)
        g.ask("agent-a", 10, "x")
        self.assertEqual(g.ask("agent-a", 1, "x")["reason"], OVER_CAP)
        self.assertEqual(g.ask("agent-a", 1, "x", day=date(2026, 1, 6))["verdict"], "GO")

    def test_would_go_negative(self):
        g = make(self.tmp)
        self.assertEqual(g.ask("agent-big", 60, "x")["reason"], WOULD_GO_NEGATIVE)

    def test_buy_it_twice(self):
        g = make(self.tmp)
        r = g.ask("agent-big", 30, "x")             # needs 60, pool 50
        self.assertEqual(r["reason"], BUY_IT_TWICE)
        self.assertIn("$60.00", r["why"])
        self.assertEqual(g.ask("agent-big", 25, "x")["verdict"], "GO")   # needs exactly 50

    def test_buy_it_twice_keep_day_allowance(self):
        g = make(self.tmp, keep_day_allowance=True)  # need = 2x + 10
        self.assertEqual(g.ask("agent-big", 21, "x")["reason"], BUY_IT_TWICE)
        self.assertEqual(g.ask("agent-big", 20, "x")["verdict"], "GO")

    def test_buy_it_twice_names_cash_limit(self):
        cash = {"balance": 70, "as_of": "2026-01-05", "emergency_reserve": 30, "paydays": ["2026-01-20"]}
        g = make(self.tmp, cash=cash)                # spendable = min(50, 40) = 40
        r = g.ask("agent-big", 25, "x")
        self.assertEqual(r["reason"], BUY_IT_TWICE)
        self.assertIn("cash after bills", r["why"])

    def test_order_is_fixed(self):
        g = make(self.tmp)
        self.assertEqual(g.ask("stranger", -1, "x")["reason"], BAD_AMOUNT)
        g.record(80, "outside", day=D5)
        self.assertEqual(g.ask("agent-a", 500, "x")["reason"], DEFICIT)   # deficit beats over-cap
        self.assertEqual(g.ask("stranger", 5, "x")["reason"], UNKNOWN_REQUESTER)  # unknown beats deficit


class Pool(Base):
    def test_rollover(self):
        g = make(self.tmp)
        self.assertEqual(g.snapshot(date(2026, 1, 1))["pool"], 10)
        self.assertEqual(g.snapshot(date(2026, 1, 10))["pool"], 100)
        g.record(30, "x", day=date(2026, 1, 3))
        self.assertEqual(g.snapshot(date(2026, 1, 2))["pool"], 20)       # spend after the day does not count
        self.assertEqual(g.snapshot(date(2026, 1, 10))["pool"], 70)
        self.assertEqual(g.snapshot(date(2025, 12, 31))["pool"], 0)      # before start

    def test_opening_balance(self):
        g = make(self.tmp, opening_balance=15)
        self.assertEqual(g.snapshot()["pool"], 65)

    def test_go_counts_no_and_dry_do_not(self):
        g = make(self.tmp)
        g.ask("agent-big", 20, "go")
        g.ask("agent-big", 999, "no")
        g.ask("agent-big", 5, "dry", dry=True)
        self.assertEqual(g.snapshot()["pool"], 30)
        verdicts = [r["verdict"] for r in g.ledger.rows()]
        self.assertEqual(verdicts, ["GO", "NO"])

    def test_void_gives_money_back(self):
        g = make(self.tmp)
        gid = g.ask("agent-a", 10, "x")["id"]
        self.assertEqual(g.snapshot()["pool"], 40)
        self.assertEqual(g.ask("agent-a", 1, "x")["reason"], OVER_CAP)
        g.void(gid, "checkout failed")
        self.assertEqual(g.snapshot()["pool"], 50)
        self.assertEqual(g.ask("agent-a", 1, "x")["verdict"], "GO")     # cap freed too
        with self.assertRaises(ValueError):
            g.void(gid, "again")
        no_id = g.ask("agent-a", 999, "x")["id"]
        with self.assertRaises(ValueError):
            g.void(no_id, "a NO has nothing to give back")

    def test_monthly_counts_full_first_charge_and_skips_cap(self):
        g = make(self.tmp)
        r = g.ask("agent-a", 20, "subscription", monthly=True)   # cap 10, but monthly skips the cap
        self.assertEqual(r["verdict"], "GO")
        self.assertTrue(r["monthly"] and r["once"])
        self.assertEqual(g.snapshot()["pool"], 30)
        self.assertEqual(g.ask("agent-a", 5, "x")["verdict"], "GO")    # did not eat the daily cap
        self.assertEqual(g.ask("agent-a", 20, "sub2", monthly=True)["reason"], BUY_IT_TWICE)  # 25 < 40

    def test_once_skips_cap_only(self):
        g = make(self.tmp)
        self.assertEqual(g.ask("agent-a", 25, "x", once=True)["verdict"], "GO")
        self.assertEqual(g.ask("agent-a", 20, "x", once=True)["reason"], BUY_IT_TWICE)


class When(Base):
    def test_when_pool_only(self):
        g = make(self.tmp)
        self.assertEqual(g.when(20), D5)                    # needs 40, has 50
        self.assertEqual(g.when(40), date(2026, 1, 8))      # needs 80: 50 + 3 days
        self.assertEqual(g.when(41), date(2026, 1, 9))      # needs 82
        g.record(25, "x", day=D5)
        self.assertEqual(g.when(40), date(2026, 1, 11))     # 25 + 6 days = 85

    def test_when_keep_day_allowance(self):
        g = make(self.tmp, keep_day_allowance=True)
        self.assertEqual(g.when(40), date(2026, 1, 9))      # needs 90

    def test_when_cash_binds(self):
        cash = {"balance": 60, "as_of": "2026-01-05", "emergency_reserve": 0, "paycheck": 100,
                "paydays": ["2026-01-12"]}
        g = make(self.tmp, cash=cash)
        # pool hits 100 on 01-10 but cash stays 60 until the paycheck lands on 01-12
        self.assertEqual(g.when(50), date(2026, 1, 12))

    def test_when_none_beyond_horizon(self):
        self.assertIsNone(make(self.tmp).when(10_000))

    def test_no_row_carries_fits_on(self):
        g = make(self.tmp)
        self.assertEqual(g.ask("agent-big", 40, "x")["fits_on"], "2026-01-08")


class Cash(Base):
    def test_only_bills_before_next_payday_count(self):
        cash = {"balance": 500, "as_of": "2026-01-05", "emergency_reserve": 100, "paydays": ["2026-01-15"],
                "bills": [{"name": "a", "amount": 200, "due": "2026-01-10"},
                          {"name": "b", "amount": 300, "due": "2026-01-16"}]}
        s = make(self.tmp, cash=cash).snapshot()
        self.assertEqual(s["cash"]["must_pay"], 200)
        self.assertEqual(s["cash"]["after_bills"], 200)
        self.assertEqual(s["spendable"], 50)               # pool is lower

    def test_bill_paid_before_day_leaves_cash(self):
        cash = {"balance": 500, "as_of": "2026-01-05", "paydays": ["2026-01-15"],
                "bills": [{"name": "a", "amount": 200, "due": "2026-01-06"}]}
        s = make(self.tmp, cash=cash).snapshot(date(2026, 1, 7))
        self.assertEqual((s["cash"]["cash"], s["cash"]["must_pay"]), (300, 0))

    def test_gate_spend_reduces_cash(self):
        cash = {"balance": 500, "as_of": "2026-01-05", "paydays": ["2026-01-15"]}
        g = make(self.tmp, cash=cash)
        g.ask("agent-big", 20, "x")
        self.assertEqual(g.snapshot()["cash"]["cash"], 480)

    def test_monthly_bill_clamps_to_month_end(self):
        bill = {"amount": 1, "day_of_month": 31}
        got = list(_bill_dates(bill, date(2026, 1, 15), date(2026, 4, 1)))
        self.assertEqual(got, [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)])

    def test_paydays_every_n_days(self):
        spec = {"paydays": {"anchor": "2026-01-09", "every_days": 14}}
        self.assertEqual(_paydays(spec, date(2026, 1, 1), date(2026, 2, 10)),
                         [date(2026, 1, 9), date(2026, 1, 23), date(2026, 2, 6)])
        self.assertEqual(_paydays(spec, date(2026, 1, 9), date(2026, 1, 23)), [date(2026, 1, 23)])


class AgentApi(Base):
    def test_require_go_returns_id(self):
        g = make(self.tmp)
        gid = require("agent-a", 5, "x", gate=g)
        self.assertTrue(gid.startswith("g"))
        self.assertEqual(g.snapshot()["pool"], 45)

    def test_require_no_raises_with_reason(self):
        g = make(self.tmp)
        with self.assertRaises(SpendDenied) as cm:
            require("agent-big", 30, "x", gate=g)
        self.assertEqual(cm.exception.reason, BUY_IT_TWICE)
        self.assertEqual(cm.exception.row["verdict"], "NO")

    def test_require_fails_closed_on_corrupt_ledger(self):
        g = make(self.tmp)
        g.ledger.path.parent.mkdir(parents=True)
        g.ledger.path.write_text('{"verdict": "GO", "amount": 5\nnot json\n', encoding="utf-8")
        with self.assertRaises(SpendDenied) as cm:
            require("agent-a", 1, "x", gate=g)
        self.assertEqual(cm.exception.reason, GATE_ERROR)

    def test_require_fails_closed_on_missing_config(self):
        import os
        old = os.environ.get("BUYITTWICE_CONFIG")
        os.environ["BUYITTWICE_CONFIG"] = str(Path(self.tmp) / "missing.json")
        try:
            with self.assertRaises(SpendDenied) as cm:
                require("agent-a", 1, "x")
            self.assertEqual(cm.exception.reason, GATE_ERROR)
        finally:
            if old is None:
                os.environ.pop("BUYITTWICE_CONFIG")
            else:
                os.environ["BUYITTWICE_CONFIG"] = old

    def test_no_override_parameter_exists(self):
        import inspect
        params = set(inspect.signature(require).parameters)
        self.assertEqual(params, {"requester", "amount", "what", "once", "monthly", "gate"})


class Cli(Base):
    def run_cli(self, *args):
        cfg = Path(self.tmp) / "cfg.json"
        cfg.write_text(json.dumps({"daily_allowance": 10, "start": "2026-01-01",
                                   "requesters": {"agent-a": 10}}), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--config", str(cfg), "--ledger", str(Path(self.tmp) / "l.jsonl"),
                         "--day", "2026-01-05", *args])
        return code, out.getvalue() + err.getvalue()

    def test_ask_exit_codes(self):
        code, out = self.run_cli("ask", "-r", "agent-a", "--amount", "5", "--what", "x")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("GO"))
        code, out = self.run_cli("ask", "-r", "agent-a", "--amount", "11", "--what", "x")
        self.assertEqual(code, 1)
        self.assertIn("daily cap", out)

    def test_when_and_status(self):
        code, out = self.run_cli("when", "40")
        self.assertEqual(code, 0)
        self.assertIn("2026-01-08", out)
        code, out = self.run_cli("status")
        self.assertIn("spendable $50.00", out)

    def test_bad_config_exit_2(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = main(["--config", str(Path(self.tmp) / "nope.json"), "status"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
