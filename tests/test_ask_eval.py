"""Offline routing accuracy eval for `doogle ask` (tests/data/ask_eval.yaml).

Strict scoring: a case passes only if the recommendation is right AND confidence is medium/high;
for out-of-scope goals Doogle must answer with confidence none/low. Thresholds sit just below the
measured v1.2 numbers so a routing regression fails CI. The held-out batch was written after the
routing changes and was not used for tuning; it is the honest estimate for unseen goals.
"""

from __future__ import annotations

import collections
from pathlib import Path

import yaml

from doogle import engine as E

CASES = yaml.safe_load((Path(__file__).parent / "data" / "ask_eval.yaml").read_text(encoding="utf-8"))
CATALOG = E.Catalog(Path(__file__).resolve().parents[1] / "blueprints")


def _score():
    ok, total, top1 = collections.Counter(), collections.Counter(), collections.Counter()
    for case in CASES:
        r = E.route_offline(case["goal"], CATALOG)
        strict = r["recommendation"] if r["confidence"] not in ("none", "low") else None
        ok[case["kind"]] += strict == case["expected"]
        top1[case["kind"]] += (r["recommendation"] == case["expected"]) if case["expected"] else (strict is None)
        total[case["kind"]] += 1
    return ok, total, top1


def test_eval_set_is_big_and_valid():
    assert len(CASES) >= 60
    ids = set(CATALOG.blueprints)
    for case in CASES:
        assert case["expected"] is None or case["expected"] in ids, case
    kinds = collections.Counter(c["kind"] for c in CASES)
    assert kinds["out_of_scope"] >= 15 and kinds["tricky"] >= 15 and kinds["held_out"] >= 15


def test_offline_routing_accuracy_thresholds():
    ok, total, top1 = _score()
    tuned = [k for k in total if k != "held_out"]
    overall = sum(ok[k] for k in tuned) / sum(total[k] for k in tuned)
    assert overall >= 0.93, f"tuned-set strict accuracy {overall:.1%}"
    assert ok["direct"] == total["direct"], "every direct goal must route correctly"
    assert ok["out_of_scope"] / total["out_of_scope"] >= 0.95, "Doogle must not guess on out-of-scope goals"
    assert ok["held_out"] / total["held_out"] >= 0.75, f"held-out strict {ok['held_out']}/{total['held_out']}"
    assert top1["held_out"] / total["held_out"] >= 0.80


def test_routing_is_deterministic():
    for case in CASES[:20]:
        assert E.route_offline(case["goal"], CATALOG) == E.route_offline(case["goal"], CATALOG)
