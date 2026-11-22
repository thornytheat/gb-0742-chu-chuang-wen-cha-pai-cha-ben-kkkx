"""按主探头裁定结果重算柜时梯度、异常段并衔接工单。"""
from collections import defaultdict

from app.modules.audit.reconcile import reconcile_tickets
from app.modules.episodes.segment import segment
from app.modules.gradient.grade import grade

HOUR = 3600


def _lower_candidates(snapshot):
    by_cabinet_hour = defaultdict(list)
    for row in snapshot["rows"]:
        if row["disposition"] != "accepted":
            continue
        window = row.get("deployment")
        if not window or window["level"] != "lower":
            continue
        by_cabinet_hour[window["cabinet"], row["measured"] // HOUR * HOUR].append(row)
    return by_cabinet_hour


def build_slots(spec, ledger, snapshot):
    """以裁定主探头为上层，下层沿用同柜同小时正本（下层无重叠部署）。"""
    evidence = {row["id"]: row for row in snapshot["rows"]}
    lowers = _lower_candidates(snapshot)
    slots = []
    for entry in ledger:
        hour = entry["hour"]
        lower_rows = lowers.get((entry["cabinet"], hour), [])
        lower = min(lower_rows, key=lambda row: (abs(row["measured"] - hour - 1800),
                                                  row["measured"], row["line"])) if lower_rows else None
        upper = evidence[entry["primary_row"]] if entry["primary_row"] else None
        if upper and lower and abs(lower["measured"] - upper["measured"]) <= spec.tolerance_seconds:
            drift = abs(lower["measured"] - upper["measured"])
            lower_id, code = lower["id"], None
        elif upper:
            drift, lower_id, code = None, None, "NO_COMPATIBLE_LOWER"
        else:
            drift, lower_id, code = None, None, "MISSING_UPPER"
        slots.append({"cabinet": entry["cabinet"], "hour": hour, "start": entry["start"],
                      "end": entry["end"], "upper": entry["primary_row"],
                      "lower": lower_id,
                      "drift": drift, "code": code,
                      "primary_probe": entry["primary_probe"], "verdict": entry["verdict"],
                      "adjudication_reason": entry["reason"]})
    return slots


def reevaluate(spec, ledger, snapshot):
    slots = build_slots(spec, ledger, snapshot)
    grades = grade(spec, slots, snapshot["rows"])
    # 裁定缺证（分歧/缺比对）优先保留缺证，不让配对侧的原因码覆盖裁定结论。
    for report, entry in zip(grades, ledger):
        report["primary_probe"] = entry["primary_probe"]
        report["verdict"] = entry["verdict"]
        report["adjudication_reason"] = entry["reason"]
        report["overlap_window"] = entry["overlap_window"]
        if entry["verdict"] == "MISSING" and entry["reason"] in {"OVERLAP_DISPUTED", "OVERLAP_UNVERIFIED"}:
            report["state"] = "MISSING"
            report["code"] = entry["reason"]
            report["axes"] = []
    episodes = segment(grades, spec.sustained_hours)
    tickets, unreported = reconcile_tickets(spec, grades, episodes)
    tally = defaultdict(int)
    for entry in ledger:
        tally[entry["reason"]] += 1
    hours = defaultdict(int)
    for report in grades:
        hours[report["state"].lower()] += 1
    handoff = {"sustained": [item["id"] for item in episodes if item["kind"] == "SUSTAINED"],
               "transient": [item["id"] for item in episodes if item["kind"] == "TRANSIENT"],
               "tickets": tickets, "unreported_episodes": unreported,
               "missing_hours": [entry["start"] for entry in ledger if entry["verdict"] == "MISSING"]}
    counts = defaultdict(int)
    for ticket in tickets:
        counts[ticket["code"]] += 1
    handoff["ticket_counts"] = dict(counts)
    return {"slots": slots, "grades": grades, "episodes": episodes, "handoff": handoff,
            "reason_tally": dict(sorted(tally.items())),
            "hour_tally": {"expected": len(ledger), "stable": hours["stable"],
                           "abnormal": hours["abnormal"], "missing": hours["missing"]}}
