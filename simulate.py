"""Generates a synthetic CI history for a radio-unit-style software product.

60 builds (two per day over a month), 30 tests in 5 components. Every failure
gets a hidden ground-truth label so the triage engine can be evaluated:
  - 3 real regressions, each introduced by a specific commit and later fixed
  - 3 flaky tests that fail at random and usually pass on rerun
  - 3 lab infrastructure outages that break many tests in one build
The CI reruns every failed test once, as many real pipelines do.

Component names are inspired by typical radio unit software areas.
All data is synthetic.
"""

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

random.seed(7)
ROOT = Path(__file__).resolve().parent

COMPONENTS = {
    "fronthaul": ["test_ecpri_link_up", "test_delay_compensation", "test_frame_timing",
                  "test_link_recovery", "test_vlan_tagging", "test_packet_loss_alarm"],
    "carrier_config": ["test_add_carrier", "test_remove_carrier", "test_bandwidth_change",
                       "test_frequency_range", "test_carrier_profile_load", "test_invalid_config_rejected"],
    "power_control": ["test_tx_power_limit", "test_power_ramp", "test_pa_calibration",
                      "test_power_report", "test_low_power_mode", "test_thermal_backoff"],
    "alarm_handling": ["test_alarm_raise", "test_alarm_clear_timing", "test_alarm_severity",
                       "test_alarm_history", "test_alarm_suppression", "test_alarm_notification"],
    "sw_upgrade": ["test_download_package", "test_verify_signature", "test_activate_package",
                   "test_rollback_timeout", "test_upgrade_status", "test_parallel_upgrade_rejected"],
}
TESTS = [(c, t) for c, ts in COMPONENTS.items() for t in ts]
AUTHORS = ["sara", "jonas", "li", "omar", "elin", "mateo"]
GENERIC_MSGS = ["Refactor {c} helpers", "Improve logging in {c}", "Update {c} unit tests",
                "Clean up {c} config parsing", "Add metrics to {c}", "Fix typo in {c} docs"]

FLAKY = {
    ("alarm_handling", "test_alarm_clear_timing"): lambda: f"TimeoutError: alarm {random.randint(1000, 9999)} not cleared within {random.choice([500, 750, 1000])} ms",
    ("sw_upgrade", "test_rollback_timeout"): lambda: f"TimeoutError: rollback did not finish in {random.randint(30, 60)} s (state=ACTIVATING)",
    ("fronthaul", "test_link_recovery"): lambda: f"AssertionError: eCPRI link not recovered after {random.randint(3, 6)} retries",
}
FLAKY_P, FLAKY_RERUN_PASS = 0.07, 0.85

REGRESSIONS = [
    {"id": "R1", "start": 12, "end": 17, "component": "power_control",
     "commit_msg": "Tune PA output calibration table",
     "fix_msg": "Revert PA calibration offset",
     "tests": {"test_tx_power_limit": lambda: f"AssertionError: tx power {random.uniform(46.1, 46.4):.1f} dBm exceeds limit 46.0 dBm"}},
    {"id": "R2", "start": 30, "end": 33, "component": "fronthaul",
     "commit_msg": "Change delay compensation to use new timestamp source",
     "decoy_msg": "Update fronthaul README",
     "fix_msg": "Fix timestamp unit (ns vs us) in delay compensation",
     "tests": {"test_delay_compensation": lambda: f"AssertionError: measured delay {random.randint(170, 260)} ns, expected <= 160 ns",
               "test_frame_timing": lambda: f"AssertionError: frame timing offset {random.randint(40, 90)} ns outside window +/-30 ns"}},
    {"id": "R3", "start": 45, "end": 51, "component": "carrier_config",
     "commit_msg": "Move carrier profiles to new schema",
     "fix_msg": "Add missing bandwidth key to carrier profile schema",
     "tests": {"test_bandwidth_change": lambda: "KeyError: 'bw_100mhz' missing in carrier profile"}},
]
INFRA = {7: "rulab-03", 23: "rulab-05", 41: "rulab-03"}


def main():
    start = datetime(2026, 10, 1, 8, 0)
    commit_no, builds = 0, []
    for b in range(1, 61):
        commits = []

        def add_commit(component, msg, truth=None):
            nonlocal commit_no
            commit_no += 1
            commits.append({"id": f"c{commit_no:04d}", "component": component, "author": random.choice(AUTHORS),
                            "message": msg.format(c=component), "truth": truth})

        for _ in range(random.randint(1, 3)):
            add_commit(random.choice(list(COMPONENTS)), random.choice(GENERIC_MSGS))
        for r in REGRESSIONS:
            if b == r["start"]:
                add_commit(r["component"], r["commit_msg"], truth=f"introduces {r['id']}")
                if "decoy_msg" in r:
                    add_commit(r["component"], r["decoy_msg"])
            if b == r["end"] + 1:
                add_commit(r["component"], r["fix_msg"], truth=f"fixes {r['id']}")
        random.shuffle(commits)

        infra_node = INFRA.get(b)
        infra_hit = set(random.sample(range(len(TESTS)), 13)) if infra_node else set()
        results = []
        for i, (comp, test) in enumerate(TESTS):
            row = {"test": f"{comp}::{test}", "component": comp, "status": "passed",
                   "log": "", "rerun": None, "truth": None}
            regression = next((r for r in REGRESSIONS if test in r["tests"] and r["component"] == comp
                               and r["start"] <= b <= r["end"]), None)
            if i in infra_hit:
                row.update(status="failed", log=f"ConnectionError: lab node {infra_node} unreachable (errno 113)",
                           rerun="failed", truth=f"infra:{b:03d}")
            elif regression:
                row.update(status="failed", log=regression["tests"][test](), rerun="failed",
                           truth=f"regression:{regression['id']}")
            elif (comp, test) in FLAKY and random.random() < FLAKY_P:
                row.update(status="failed", log=FLAKY[(comp, test)](),
                           rerun="passed" if random.random() < FLAKY_RERUN_PASS else "failed", truth="flaky")
            results.append(row)
        builds.append({"id": f"b{b:03d}", "time": (start + timedelta(hours=12 * (b - 1))).isoformat(),
                       "commits": commits, "results": results})

    out = ROOT / "data" / "ci_history.json"
    out.write_text(json.dumps({"builds": builds}, indent=1))
    fails = sum(r["status"] == "failed" for bl in builds for r in bl["results"])
    print(f"Wrote {out.relative_to(ROOT)}: {len(builds)} builds, {len(TESTS)} tests, {fails} failed test runs")


if __name__ == "__main__":
    main()
