"""The spend gate: a purchase clears only when you could pay for it twice.

Model
-----
* A fixed daily allowance accrues into a POOL from a start date. Unspent money rolls forward.
* Every GO the gate gives is written to an append-only JSONL ledger and counts as spend that day.
  Spend made outside the gate can be added with `record`, so the pool stays honest.
* Optional CASH side: if the config has a `cash` block, spendable = min(pool, cash - must-pay bills due
  before the next payday - emergency reserve). Without it, spendable = pool.
* Buy-it-twice: a GO needs spendable >= 2 x price (+ one day's allowance if `keep_day_allowance`).
* Requesters (agents, teams, scripts) each have a daily cap. `once` / `monthly` asks skip the cap
  but every other rule still binds.
* There is no override. See README for why.

NO reasons are checked in this fixed order; the first that fails is the answer:
    bad-amount, unknown-requester, deficit, used-up, over-cap, would-go-negative, buy-it-twice
"""
from __future__ import annotations

import calendar
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

EPS = 1e-9
HORIZON_DAYS = 366

BAD_AMOUNT = "bad-amount"
UNKNOWN_REQUESTER = "unknown-requester"
DEFICIT = "deficit"
USED_UP = "used-up"
OVER_CAP = "over-cap"
WOULD_GO_NEGATIVE = "would-go-negative"
BUY_IT_TWICE = "buy-it-twice"
GATE_ERROR = "gate-error"

REASON_ORDER = (BAD_AMOUNT, UNKNOWN_REQUESTER, DEFICIT, USED_UP, OVER_CAP, WOULD_GO_NEGATIVE, BUY_IT_TWICE)


class SpendDenied(Exception):
    """Raised by require() on any NO, and when the gate cannot answer (fail closed)."""

    def __init__(self, reason: str, message: str, row: dict | None = None):
        super().__init__(message)
        self.reason = reason
        self.row = row or {}


class ConfigError(ValueError):
    pass


def _d(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x))


def money(x: float, sym: str = "$") -> str:
    return f"-{sym}{abs(x):,.2f}" if x < 0 else f"{sym}{x:,.2f}"


# ---------------------------------------------------------------------------------------------- config

@dataclass
class Config:
    daily_allowance: float
    start: date
    opening_balance: float = 0.0
    keep_day_allowance: bool = False
    currency: str = "$"
    requesters: dict[str, float] = field(default_factory=dict)   # name -> daily cap; "*" = any other name
    cash: dict | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "Config":
        try:
            rate = float(raw["daily_allowance"])
            start = _d(raw["start"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"config needs daily_allowance (number) and start (YYYY-MM-DD): {exc}") from exc
        if rate <= 0:
            raise ConfigError("daily_allowance must be more than 0")
        reqs = {}
        for name, v in (raw.get("requesters") or {}).items():
            cap = v.get("daily_cap") if isinstance(v, dict) else v
            reqs[str(name)] = float(cap)
        cash = raw.get("cash")
        if cash is not None:
            for k in ("balance", "as_of"):
                if k not in cash:
                    raise ConfigError(f"cash block needs {k!r}")
        return cls(daily_allowance=rate, start=start, opening_balance=float(raw.get("opening_balance", 0)),
                   keep_day_allowance=bool(raw.get("keep_day_allowance", False)),
                   currency=str(raw.get("currency", "$")), requesters=reqs, cash=cash)

    @classmethod
    def load(cls, path: str | os.PathLike) -> "Config":
        p = Path(path)
        try:
            return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except OSError as exc:
            raise ConfigError(f"cannot read config {p}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config {p} is not valid JSON: {exc}") from exc


def default_config_path() -> Path:
    return Path(os.environ.get("BUYITTWICE_CONFIG") or "buyittwice.json")


def default_ledger_path() -> Path:
    if os.environ.get("BUYITTWICE_LEDGER"):
        return Path(os.environ["BUYITTWICE_LEDGER"])
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg and not Path("state").exists():
        return Path(xdg) / "buyittwice" / "ledger.jsonl"
    return Path("state") / "ledger.jsonl"


# ---------------------------------------------------------------------------------------------- ledger

class Ledger:
    """Append-only JSONL. Rows: GO, NO, VOID (gives a GO back), RECORD (spend made outside the gate)."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                # A row we cannot read might be a spend. Refuse to guess the pool.
                raise ConfigError(f"ledger {self.path} line {n} is not JSON") from exc
        return out

    def append(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    def spends(self) -> list[dict]:
        """Live spend rows: GO not voided, plus RECORD."""
        rows = self.rows()
        voided = {r.get("voids") for r in rows if r.get("verdict") == "VOID"}
        return [r for r in rows
                if (r.get("verdict") == "GO" and r.get("id") not in voided) or r.get("verdict") == "RECORD"]


# ---------------------------------------------------------------------------------------------- cash side

def _bill_dates(bill: dict, lo: date, hi: date) -> Iterable[date]:
    """Due dates of one bill in [lo, hi). A bill has `due` (one date) or `day_of_month` (monthly)."""
    if "due" in bill:
        d = _d(bill["due"])
        if lo <= d < hi:
            yield d
        return
    dom = int(bill["day_of_month"])
    y, m = lo.year, lo.month
    while True:
        last = calendar.monthrange(y, m)[1]
        d = date(y, m, min(dom, last))
        if d >= hi:
            return
        if d >= lo:
            yield d
        m += 1
        if m == 13:
            y, m = y + 1, 1


def _paydays(cash: dict, lo: date, hi: date) -> list[date]:
    """Paydays in (lo, hi]. `paydays` is a list of dates or {"anchor": date, "every_days": N}."""
    spec = cash.get("paydays")
    if not spec:
        return []
    if isinstance(spec, list):
        return sorted(d for d in map(_d, spec) if lo < d <= hi)
    anchor, step = _d(spec["anchor"]), int(spec["every_days"])
    if step <= 0:
        raise ConfigError("paydays.every_days must be more than 0")
    d = anchor + timedelta(days=step * ((lo - anchor).days // step))   # the last payday on or before lo
    out = []
    while d <= hi:
        if d > lo:
            out.append(d)
        d += timedelta(days=step)
    return out


def _next_payday(cash: dict, day: date) -> date:
    nxt = _paydays(cash, day, day + timedelta(days=HORIZON_DAYS))
    return nxt[0] if nxt else day + timedelta(days=int(cash.get("window_days", 30)))


def cash_side(cash: dict, day: date, spends: list[dict]) -> dict:
    """Cash after must-pay bills due before the next payday and the emergency reserve, on `day`.

    `balance` is the cash at the start of `as_of`. From there: gate spends since as_of come out, bills due
    before `day` are assumed paid, and `paycheck` (optional, default 0) is assumed to land on each payday."""
    as_of = _d(cash["as_of"])
    day = max(day, as_of)
    bills = cash.get("bills") or []
    spent = sum(float(r["amount"]) for r in spends if as_of <= _d(r["day"]) <= day)
    paid = sum(float(b["amount"]) for b in bills for _ in _bill_dates(b, as_of, day))
    income = float(cash.get("paycheck", 0)) * len(_paydays(cash, as_of, day))
    now = float(cash["balance"]) - spent - paid + income
    nxt = _next_payday(cash, day)
    due = [(b.get("name", "bill"), float(b["amount"]), d.isoformat())
           for b in bills for d in _bill_dates(b, day, nxt)]
    must = sum(x[1] for x in due)
    reserve = float(cash.get("emergency_reserve", 0))
    return {"cash": round(now, 2), "must_pay": round(must, 2), "bills_due": due, "next_payday": nxt.isoformat(),
            "reserve": reserve, "after_bills": round(now - must - reserve, 2)}


# ---------------------------------------------------------------------------------------------- gate

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Gate:
    def __init__(self, config: Config, ledger: Ledger, today: Callable[[], date] = date.today):
        self.cfg = config
        self.ledger = ledger
        self.today = today

    @classmethod
    def from_paths(cls, config: str | os.PathLike | None = None, ledger: str | os.PathLike | None = None) -> "Gate":
        return cls(Config.load(config or default_config_path()), Ledger(ledger or default_ledger_path()))

    # -- numbers

    def need_for(self, amount: float) -> float:
        return 2 * amount + (self.cfg.daily_allowance if self.cfg.keep_day_allowance else 0.0)

    def accrued(self, day: date) -> float:
        days = (day - self.cfg.start).days + 1
        return self.cfg.opening_balance + self.cfg.daily_allowance * max(days, 0)

    def snapshot(self, day: date | None = None, spends: list[dict] | None = None) -> dict:
        day = day or self.today()
        spends = self.ledger.spends() if spends is None else spends
        spent = sum(float(r["amount"]) for r in spends if _d(r["day"]) <= day)
        pool = round(self.accrued(day) - spent, 2)
        snap = {"day": day.isoformat(), "rate": self.cfg.daily_allowance, "accrued": round(self.accrued(day), 2),
                "spent": round(spent, 2), "pool": pool, "spendable": pool, "short": False}
        if self.cfg.cash:
            c = cash_side(self.cfg.cash, day, spends)
            snap["cash"] = c
            snap["spendable"] = round(min(pool, c["after_bills"]), 2)
            snap["short"] = c["after_bills"] < 0
        return snap

    def cap_for(self, requester: str) -> float | None:
        if requester in self.cfg.requesters:
            return self.cfg.requesters[requester]
        return self.cfg.requesters.get("*")

    def spent_today(self, requester: str, day: date, spends: list[dict] | None = None) -> float:
        spends = self.ledger.spends() if spends is None else spends
        return round(sum(float(r["amount"]) for r in spends
                         if r.get("verdict") == "GO" and r.get("requester") == requester
                         and _d(r["day"]) == day and not r.get("once")), 2)

    # -- the decision

    def decide(self, requester: str, amount: float, day: date | None = None, once: bool = False) -> tuple[bool, str, str, dict]:
        """(go, reason_code, why, snapshot). reason_code is "" on GO."""
        day = day or self.today()
        spends = self.ledger.spends()
        p = self.snapshot(day, spends)
        m = lambda x: money(x, self.cfg.currency)  # noqa: E731
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return False, BAD_AMOUNT, f"amount {amount!r} is not a number", p
        if not amount > 0 or amount != amount:
            return False, BAD_AMOUNT, "amount must be more than 0", p
        cap = self.cap_for(requester)
        if cap is None:
            return False, UNKNOWN_REQUESTER, (f"unknown requester {requester!r}: configured are "
                                              f"{', '.join(sorted(self.cfg.requesters)) or 'none'}"), p
        bal = p["spendable"]
        if p["short"] or bal < 0:
            why = (f"cash after must-pay bills and reserve is {m(p['cash']['after_bills'])}" if p["short"]
                   else f"the pool is {m(p['pool'])}: spent past what accrued")
            return False, DEFICIT, f"in deficit: {why}. No spends until it is back above 0", p
        if bal <= 0:
            return False, USED_UP, f"the pool is used up ({m(bal)}). Next {m(self.cfg.daily_allowance)} lands tomorrow", p
        used = self.spent_today(requester, day, spends)
        p.update(cap=cap, requester_spent_today=used)
        if not once and used + amount > cap + EPS:
            return False, OVER_CAP, (f"{requester} daily cap: {m(used)} already today + {m(amount)} "
                                     f"is over {m(cap)}"), p
        if bal - amount < -EPS:
            return False, WOULD_GO_NEGATIVE, f"would push spendable negative: {m(bal)} left, asked {m(amount)}", p
        need = self.need_for(amount)
        if bal < need - EPS:
            limit = (f"pool {m(p['pool'])}" if bal == p["pool"]
                     else f"pool {m(p['pool'])} but only {m(bal)} cash after bills and reserve")
            return False, BUY_IT_TWICE, f"{m(amount)} needs {m(need)} spendable; {limit}", p
        return True, "", f"spendable {m(bal)} -> {m(bal - amount)}", p

    def ask(self, requester: str, amount: float, what: str, *, once: bool = False, monthly: bool = False,
            dry: bool = False, day: date | None = None) -> dict:
        """Decide and (unless dry) record. monthly=True: `amount` is the full first charge; skips the daily cap."""
        once = once or monthly
        day = day or self.today()
        ok, reason, why, p = self.decide(requester, amount, day, once)
        try:
            amt = round(float(amount), 2)
        except (TypeError, ValueError):
            amt = 0.0
        row = {"id": "g" + uuid.uuid4().hex[:10], "at": _now_iso(), "day": day.isoformat(),
               "requester": requester, "amount": amt, "what": what, "verdict": "GO" if ok else "NO",
               "reason": reason, "why": why, "spendable_before": p["spendable"], "once": once,
               "monthly": monthly, "dry": dry}
        if not ok and reason not in (BAD_AMOUNT, UNKNOWN_REQUESTER, OVER_CAP):   # a cap resets tomorrow
            w = self.when(amt, day=day)
            row["fits_on"] = w.isoformat() if w else None
        if not dry:
            self.ledger.append(row)
        return row

    def void(self, go_id: str, why: str, day: date | None = None) -> dict:
        rows = self.ledger.rows()
        if any(r.get("verdict") == "VOID" and r.get("voids") == go_id for r in rows):
            raise ValueError(f"{go_id} is already voided")
        g = next((r for r in rows if r.get("id") == go_id and r.get("verdict") == "GO"), None)
        if not g:
            raise ValueError(f"no GO with id {go_id}")
        row = {"id": "v" + uuid.uuid4().hex[:10], "at": _now_iso(), "day": (day or self.today()).isoformat(),
               "verdict": "VOID", "voids": go_id, "amount": g["amount"], "requester": g.get("requester"), "why": why}
        self.ledger.append(row)
        return row

    def record(self, amount: float, what: str, day: date | None = None) -> dict:
        """Spend made outside the gate (a human bought something). Counts against the pool, no decision."""
        if not float(amount) > 0:
            raise ValueError("amount must be more than 0")
        row = {"id": "r" + uuid.uuid4().hex[:10], "at": _now_iso(), "day": (day or self.today()).isoformat(),
               "verdict": "RECORD", "amount": round(float(amount), 2), "what": what}
        self.ledger.append(row)
        return row

    def when(self, price: float, day: date | None = None) -> date | None:
        """First date from `day` (included) the spendable amount reaches need_for(price) at zero other spend."""
        day = day or self.today()
        need = self.need_for(float(price))
        spends = self.ledger.spends()
        for k in range(HORIZON_DAYS + 1):
            d = day + timedelta(days=k)
            p = self.snapshot(d, spends)
            if not p["short"] and p["spendable"] >= need - EPS:
                return d
        return None

    def status(self, day: date | None = None) -> dict:
        day = day or self.today()
        spends = self.ledger.spends()
        s = self.snapshot(day, spends)
        s["requesters"] = {n: {"cap": c, "spent_today": self.spent_today(n, day, spends)}
                           for n, c in sorted(self.cfg.requesters.items())}
        s["state"] = ("DEFICIT" if s["short"] or s["spendable"] < 0 else
                      "USED UP" if s["spendable"] <= 0 else "OPEN")
        return s


# ---------------------------------------------------------------------------------------------- agent API

def require(requester: str, amount: float, what: str, once: bool = False, monthly: bool = False,
            gate: Gate | None = None) -> str:
    """Call before any purchase. GO -> the GO id (spend recorded). NO, or a gate that errors -> SpendDenied.

    There is no override parameter, by design."""
    try:
        g = gate or Gate.from_paths()
        row = g.ask(requester, amount, what, once=once, monthly=monthly)
    except Exception as exc:  # noqa: BLE001  fail closed: a gate that cannot answer is a NO
        raise SpendDenied(GATE_ERROR, f"spend gate could not answer ({type(exc).__name__}: {exc})") from exc
    if row["verdict"] != "GO":
        raise SpendDenied(row["reason"], f"NO ({row['reason']}) {requester} {amount} for {what}: {row['why']}", row)
    return row["id"]


def check(requester: str, amount: float, what: str, once: bool = False, monthly: bool = False,
          gate: Gate | None = None) -> tuple[bool, str]:
    """Dry run: (go, why). Nothing recorded."""
    g = gate or Gate.from_paths()
    row = g.ask(requester, amount, what, once=once, monthly=monthly, dry=True)
    return row["verdict"] == "GO", row["why"]


def void(go_id: str, why: str, gate: Gate | None = None) -> dict:
    """The purchase never charged: give the money back to the pool."""
    return (gate or Gate.from_paths()).void(go_id, why)
