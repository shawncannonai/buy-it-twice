"""An agent's buy path: ask the gate first, give the money back if checkout fails.

    python3 examples/agent_purchase.py --config examples/pool_only.json --ledger /tmp/bit-example.jsonl
"""
from __future__ import annotations

import argparse
import sys

from buyittwice import Gate, SpendDenied, require, void


def checkout(item: str, amount: float) -> bool:
    """Stand-in for a real payment call. Returns False to show the void path."""
    print(f"  (pretend checkout: {item} for {amount:.2f})")
    return "fail" not in item


def buy(gate: Gate, requester: str, amount: float, item: str) -> None:
    try:
        gid = require(requester, amount, item, gate=gate)
    except SpendDenied as e:
        print(f"{item}: refused ({e.reason}): {e}")
        return
    if checkout(item, amount):
        print(f"{item}: bought, gate id {gid}")
    else:
        void(gid, "checkout failed, nothing charged", gate=gate)
        print(f"{item}: checkout failed, money returned to the pool")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="examples/pool_only.json")
    ap.add_argument("--ledger", default="state/example-ledger.jsonl")
    o = ap.parse_args()
    g = Gate.from_paths(o.config, o.ledger)
    buy(g, "agent-ops", 3.00, "small api top-up")
    buy(g, "agent-ops", 3.00, "second top-up (over the daily cap)")
    buy(g, "agent-ops", 2.00, "top-up that will fail at checkout")
    print(g.status()["spendable"], "spendable now")
    return 0


if __name__ == "__main__":
    sys.exit(main())
