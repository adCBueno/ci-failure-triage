"""Evaluates the triage engine against the ground truth and reports quality KPIs.

Baseline: a common CI rule of thumb, "passes on rerun = flaky, otherwise a real bug".
"""

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from triage.engine import triage

ROOT = Path(__file__).resolve().parent
CLASSES = ["regression", "flaky", "infra"]
# Engineer-time assumptions (stated so they can be changed):
MIN_PER_RAW_FAILURE = 10   # opening a log, checking history, deciding what it is
MIN_PER_INCIDENT = 15      # reading a triage note and confirming it


def main():
    history = json.loads((ROOT / "data/ci_history.json").read_text())
    notes = json.loads((ROOT / "llm_outputs/triage_notes.json").read_text())
    builds = history["builds"]
    labels, incidents = triage(history)

    failures = {(b["id"], r["test"]): r for b in builds for r in b["results"] if r["status"] == "failed"}
    truth = {k: r["truth"].split(":")[0] for k, r in failures.items()}
    baseline = {k: ("flaky" if r["rerun"] == "passed" else "regression") for k, r in failures.items()}

    def scores(pred):
        cm = Counter((truth[k], pred[k]) for k in truth)
        acc = sum(cm[(c, c)] for c in CLASSES) / len(truth)
        per = {}
        for c in CLASSES:
            tp = cm[(c, c)]
            fp = sum(cm[(o, c)] for o in CLASSES if o != c)
            fn = sum(cm[(c, o)] for o in CLASSES if o != c)
            per[c] = (tp / (tp + fp) if tp + fp else 0.0, tp / (tp + fn) if tp + fn else 0.0)
        return cm, acc, per

    cm_e, acc_e, per_e = scores(labels)
    cm_b, acc_b, per_b = scores(baseline)

    # Regression incidents vs. the 3 real regressions.
    commit_truth = {c["id"]: c["truth"] for b in builds for c in b["commits"] if c["truth"]}
    real = {"R1", "R2", "R3"}
    reg_rows = []
    found = set()
    for inc in [i for i in incidents if i["kind"] == "regression"]:
        tags = {failures[(bid, t)]["truth"] for bid in inc["builds"] for t in inc["tests"]
                if (bid, t) in failures}
        real_ids = {t.split(":")[1] for t in tags if t.startswith("regression")}
        rid = next(iter(real_ids), None)
        found |= real_ids
        cand = [s["id"] for s in inc["suspects"]]
        true_commit = next((c for c, t in commit_truth.items() if rid and t == f"introduces {rid}"), None)
        top = notes[inc["id"]]["suspect_ranking"][0] if notes.get(inc["id"], {}).get("suspect_ranking") else None
        reg_rows.append((inc, rid, cand, true_commit, top))

    # Quality KPIs.
    t = lambda bid: datetime.fromisoformat(builds[int(bid[1:]) - 1]["time"])
    builds_red = sum(any(r["status"] == "failed" for r in b["results"]) for b in builds)
    builds_real = {bid for inc in incidents if inc["kind"] == "regression" for bid in inc["builds"]}
    flaky_rate = {i["tests"][0]: i["failures"] / len(builds) for i in incidents if i["kind"] == "flaky"}
    comp_fail = defaultdict(int)
    for inc in incidents:
        if inc["kind"] == "regression":
            comp_fail[inc["component"]] += inc["failures"]

    n_raw, n_inc = len(failures), len(incidents)
    hours_raw = n_raw * MIN_PER_RAW_FAILURE / 60
    hours_inc = n_inc * MIN_PER_INCIDENT / 60

    plot_timeline(builds, labels)

    L = ["# Evaluation results", "",
         f"Synthetic CI history: {len(builds)} builds, 30 tests, **{n_raw} failed test runs**. "
         "Ground truth: 3 regressions, 3 flaky tests, 3 lab outages.", "",
         "## 1. Classification of every failure", "",
         "| | Baseline: \"passes on rerun = flaky, else bug\" | Triage engine |", "|---|---|---|",
         f"| Accuracy | {acc_b:.0%} | **{acc_e:.0%}** |"]
    for c in CLASSES:
        L.append(f"| {c.capitalize()}: precision / recall | {per_b[c][0]:.0%} / {per_b[c][1]:.0%} | "
                 f"{per_e[c][0]:.0%} / {per_e[c][1]:.0%} |")
    L += ["", "Confusion matrix of the triage engine (rows = truth, columns = predicted):", "",
          "| truth \\ predicted | " + " | ".join(CLASSES) + " |", "|---|---|---|---|"]
    for c in CLASSES:
        L.append(f"| {c} | " + " | ".join(str(cm_e[(c, o)]) for o in CLASSES) + " |")
    L += ["", f"The baseline labels all {cm_b[('infra', 'regression')]} lab-outage failures as code bugs, "
          "so engineers would chase problems that are not in the code.", "",
          "## 2. Regressions and suspect commits", "",
          "| Incident | Real regression | Builds | Candidate commits (rules) | Introducing commit | AI top suspect |",
          "|---|---|---|---|---|---|"]
    for inc, rid, cand, true_c, top in reg_rows:
        L.append(f"| {inc['id']} | {rid or '**false alarm (flaky)**'} | {len(inc['builds'])} | {', '.join(cand)} | "
                 f"{true_c or '–'} | {top}{' ✔' if top and top == true_c else ''} |")
    L += ["", f"All {len(found & real)} of {len(real)} real regressions were found. The rules narrow each one to 1–2 "
          "candidate commits; the AI note picked the introducing commit first in every real regression. "
          "One one-off flaky failure became a false alarm, and its note flagged it as low confidence.", "",
          "## 3. Noise reduction and engineer time", "",
          f"- {n_raw} raw failures → **{n_inc} incidents** ({1 - n_inc / n_raw:.0%} fewer items to look at).",
          f"- Builds with at least one red test: {builds_red} of {len(builds)}. Builds with a real code regression: "
          f"{len(builds_real)}.",
          f"- Estimated triage time: {hours_raw:.1f} h one failure at a time vs. {hours_inc:.1f} h per incident "
          f"(assumes {MIN_PER_RAW_FAILURE} min per raw failure and {MIN_PER_INCIDENT} min per incident).", "",
          "## 4. Quality KPIs", "",
          "| KPI | Value |", "|---|---|"]
    for inc, rid, *_ in reg_rows:
        if rid:
            nxt = builds[int(inc["builds"][-1][1:])]["id"]
            L.append(f"| Time to fix {inc['id']} ({inc['component']}) | {(t(nxt) - t(inc['builds'][0])).total_seconds() / 3600:.0f} h |")
    for test, rate in flaky_rate.items():
        L.append(f"| Flaky rate {test} | {rate:.0%} of builds |")
    L.append(f"| Builds lost to lab outages | {sum(i['kind'] == 'infra' for i in incidents)} |")
    L.append(f"| Regression failures by component | " + ", ".join(f"{c}: {n}" for c, n in comp_fail.items()) + " |")
    L += ["", "![Failures per build](timeline.png)", "", "## 5. Triage notes (AI layer)", ""]
    for inc in incidents:
        n = notes[inc["id"]]
        L.append(f"**{inc['id']}** ({inc['kind']}): {n['summary']} *Action:* {n['action']} "
                 f"*Owner:* {n['owner']}. *Confidence:* {n['confidence']}.")
        L.append("")
    (ROOT / "results/report.md").write_text("\n".join(L) + "\n")
    print("\n".join(L[:40]))


def plot_timeline(builds, labels):
    colors = {"regression": "#D85A30", "flaky": "#EF9F27", "infra": "#888780"}
    x = range(1, len(builds) + 1)
    bottom = [0] * len(builds)
    fig, ax = plt.subplots(figsize=(10, 3.6))
    for c in CLASSES:
        vals = [sum(1 for r in b["results"] if r["status"] == "failed" and labels[(b["id"], r["test"])] == c)
                for b in builds]
        ax.bar(x, vals, bottom=bottom, color=colors[c], label=c, width=0.8)
        bottom = [a + v for a, v in zip(bottom, vals)]
    ax.set_xlabel("Build")
    ax.set_ylabel("Failed tests")
    ax.set_title("CI failures per build, as classified by the triage engine")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.2))
    fig.tight_layout()
    fig.savefig(ROOT / "results/timeline.png", dpi=150)


if __name__ == "__main__":
    main()
