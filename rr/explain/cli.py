"""Explain committed decisions. Read-only over `decision`; writes only to
`decision_explanation`, a separate table, so it cannot alter what it renders.

    python -m rr.explain.cli --limit 40 --resolver openai
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib

from rr.db.conn import connect
from rr.explain.explainer import Explainer


def load_decisions(conn, run_id: str | None, limit: int) -> list[dict]:
    sql = """SELECT id, payment_intent_id, slot, arm, chosen_action, chosen_channel,
                    scheduled_for_h, candidate_set, score_basis, decision_reason_code,
                    binding_constraint, taxonomy_version, model_version, policy_version
             FROM decision WHERE arm = 'treatment'
               AND chosen_action <> 'no_action' {run} ORDER BY id LIMIT %s"""
    sql = sql.format(run="AND run_id = %s" if run_id else "")
    args = ((run_id, limit) if run_id else (limit,))
    with conn.cursor() as cur:
        cur.execute(sql, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--resolver", default="offline",
                    choices=("offline", "openai", "anthropic", "none"))
    ap.add_argument("--models", type=pathlib.Path, default=pathlib.Path("models"))
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()

    resolver = None
    if args.resolver != "none":
        if args.resolver == "offline":
            print("  --resolver offline has no explainer stand-in; use `none` for the\n"
                  "  LLM-deleted configuration, or `openai` for live calls.")
            args.resolver = "none"
        else:
            from rr.normalize.resolvers import build_resolver
            resolver = build_resolver(args.resolver)

    ex = Explainer(resolver, cache_path=args.models / "explainer_cache.json")
    with connect() as conn:
        rows = load_decisions(conn, args.run_id, args.limit)
        print(f"\n=== explaining {len(rows)} committed decisions "
              f"(resolver: {resolver.model_id if resolver else 'NONE - templated only'}) ===\n")
        with conn.cursor() as cur:
            for rec in rows:
                out = ex.explain(rec)
                cur.execute(
                    """INSERT INTO decision_explanation (decision_id, text, source, reject_reason)
                       VALUES (%s,%s,%s,%s) ON CONFLICT (decision_id) DO NOTHING""",
                    (rec["id"], out.text, out.source, out.reject_reason))
        conn.commit()
    ex.save(args.models / "explainer_calls.jsonl")

    by_source = collections.Counter()
    for rec in rows:
        pass
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT source, count(*) FROM decision_explanation GROUP BY 1")
        by_source = dict(cur.fetchall())

    print(f"  {'source':<26}{'n':>6}")
    for k, v in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<26}{v:>6}")
    print(f"\n  live calls      : {ex.live_calls}")
    print(f"  rejections      : {len(ex.rejections)}  "
          f"(rate {ex.rejection_rate:.1%} of live calls)")
    if ex.rejections:
        why = collections.Counter(r["reason"] for r in ex.rejections)
        for reason, n in why.most_common():
            print(f"    {reason:<28}{n}")
    print(f"  cost            : ${ex.total_cost_usd:.4f}")
    print("\n  A nonzero disclosed rejection rate is the point: it is evidence the")
    print("  validator runs. Every rejected draft was replaced by the templated")
    print("  rendering, which is built from the record alone.\n")


if __name__ == "__main__":
    main()
