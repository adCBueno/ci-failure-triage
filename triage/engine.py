"""Triage engine: turns raw CI failures into a short list of incidents.

Deterministic steps first, AI only where it adds value:
  1. Signature: normalise each failure log (mask numbers, ids, node names) so
     failures with the same cause share one signature.
  2. Infrastructure: one signature failing in 3+ components in the same build
     points to the lab, not the code.
  3. Flaky: a failure that passes on rerun with no code change.
  4. Regression: a failure that persists on rerun. Consecutive failures of the
     same test with the same signature form one incident, and the suspect commit
     is the change to that component in the first failing build.
  5. History check: a one-build "regression" that disappears in the next build
     without any fix to its component is reclassified as flaky.
The AI layer (llm_outputs/triage_notes.json) then writes a short note per incident.
"""

import re
from collections import defaultdict

INFRA_MIN_COMPONENTS = 3


def signature(log):
    s = re.sub(r"0x[0-9a-fA-F]+", "<HEX>", log)
    s = re.sub(r"\d+(\.\d+)?", "<N>", s)
    return s


def triage(history):
    builds = history["builds"]
    index = {b["id"]: i for i, b in enumerate(builds)}
    labels = {}          # (build id, test) -> "infra" | "flaky" | "regression"
    infra_events = []

    for b in builds:
        fails = [r for r in b["results"] if r["status"] == "failed"]
        by_sig = defaultdict(list)
        for r in fails:
            by_sig[signature(r["log"])].append(r)
        for sig, rows in by_sig.items():
            if len({r["component"] for r in rows}) >= INFRA_MIN_COMPONENTS:
                infra_events.append({"id": f"I-{b['id']}", "kind": "infra", "builds": [b["id"]],
                                     "signature": sig, "failures": len(rows),
                                     "components": sorted({r["component"] for r in rows})})
                for r in rows:
                    labels[(b["id"], r["test"])] = "infra"
        for r in fails:
            key = (b["id"], r["test"])
            if key not in labels:
                labels[key] = "flaky" if r["rerun"] == "passed" else "regression"

    # Group persistent failures into regression incidents.
    open_chain, chains = {}, []
    for b in builds:
        status = {r["test"]: r for r in b["results"]}
        for test, r in status.items():
            key = (b["id"], test)
            sig = signature(r["log"]) if r["status"] == "failed" else None
            if labels.get(key) == "regression":
                chain = open_chain.get(test)
                if chain and chain["signature"] == sig:
                    chain["builds"].append(b["id"])
                else:
                    chain = {"test": test, "component": r["component"], "signature": sig, "builds": [b["id"]]}
                    open_chain[test] = chain
                    chains.append(chain)
            elif labels.get(key) == "infra":
                continue  # the lab hid this result; keep the chain open
            else:
                open_chain.pop(test, None)

    # Known flaky signatures: (test, signature) pairs that already passed on rerun
    # in an EARLIER build. Only past builds are used, as a live pipeline would.
    confirmed_flaky = defaultdict(list)   # (test, signature) -> build indexes
    for b in builds:
        for r in b["results"]:
            if r["status"] == "failed" and labels.get((b["id"], r["test"])) == "flaky":
                confirmed_flaky[(r["test"], signature(r["log"]))].append(index[b["id"]])

    # History checks: a short-lived "regression" is really flaky if it matches a
    # known flaky signature seen before, or if it disappears without any fix.
    kept = []
    for c in chains:
        first, last = index[c["builds"][0]], index[c["builds"][-1]]
        nxt = builds[last + 1] if last + 1 < len(builds) else None
        fixed_by_commit = nxt and any(cm["component"] == c["component"] for cm in nxt["commits"])
        seen_before = any(i < first for i in confirmed_flaky.get((c["test"], c["signature"]), []))
        if len(c["builds"]) == 1 and (seen_before or (nxt and not fixed_by_commit)):
            for bid in c["builds"]:
                labels[(bid, c["test"])] = "flaky"
        else:
            kept.append(c)

    # Merge chains that start in the same build, in the same component, with the
    # same suspects: one change breaking two tests is one incident.
    regressions = []
    for c in kept:
        first = builds[index[c["builds"][0]]]
        suspects = [cm for cm in first["commits"] if cm["component"] == c["component"]] or first["commits"]
        same = next((g for g in regressions if g["builds"][0] == c["builds"][0]
                     and g["component"] == c["component"]), None)
        if same:
            same["tests"].append(c["test"])
            same["signatures"].append(c["signature"])
            same["builds"] = sorted(set(same["builds"]) | set(c["builds"]))
            same["failures"] += len(c["builds"])
        else:
            regressions.append({"kind": "regression", "component": c["component"], "tests": [c["test"]],
                                "signatures": [c["signature"]], "builds": list(c["builds"]),
                                "failures": len(c["builds"]),
                                "suspects": [{k: cm[k] for k in ("id", "author", "message")} for cm in suspects]})
    for i, g in enumerate(sorted(regressions, key=lambda g: g["builds"][0]), 1):
        g["id"] = f"R-{i:02d}"

    flaky = defaultdict(lambda: {"kind": "flaky", "builds": [], "signatures": set(), "rerun_failed": 0})
    for (bid, test), lab in labels.items():
        if lab == "flaky":
            f = flaky[test]
            f["builds"].append(bid)
            row = next(r for r in builds[index[bid]]["results"] if r["test"] == test)
            f["signatures"].add(signature(row["log"]))
            f["rerun_failed"] += row["rerun"] == "failed"
    flaky_incidents = []
    for test, f in sorted(flaky.items()):
        flaky_incidents.append({"id": f"F-{test.split('::')[1]}", "kind": "flaky", "tests": [test],
                                "builds": sorted(f["builds"]), "failures": len(f["builds"]),
                                "signatures": sorted(f["signatures"]), "rerun_failed": f["rerun_failed"]})

    incidents = sorted(regressions, key=lambda g: g["id"]) + flaky_incidents + infra_events
    return labels, incidents
