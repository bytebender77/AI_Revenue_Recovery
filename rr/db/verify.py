"""make verify-chain -- recompute the hash chain over every ledger entry."""
from __future__ import annotations

import sys

from rr.db import ledger
from rr.db.conn import connect


def main() -> None:
    with connect() as conn:
        ok, checked, reason = ledger.verify(conn)
    if ok:
        print(f"  ledger chain OK -- {checked} entries verified end to end")
        sys.exit(0)
    print(f"  LEDGER CHAIN BROKEN after {checked} entries: {reason}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
