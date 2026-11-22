"""按柜时裁定换探头后的主探头。

原排查流水线把同柜同层的重叠部署整段判为位置冲突，重叠期柜时全部缺证。
本模块不改原归卷证据，只在其之上逐小时做重叠比对：双探头读数一致才采信、
分歧或缺比对时保留缺证，并按裁定后的主探头重新配准、判级、切段和勾稽工单。
"""
from collections import defaultdict
from decimal import Decimal, localcontext

from app.reasons import REASONS
from app.timebase import seconds, utc

HOUR = 3600


def _overlap_spans(deployments, cabinet, level):
    spans = []
    windows = [item for item in deployments
               if item["cabinet"] == cabinet and item["level"] == level]
    for i, first in enumerate(windows):
        for second in windows[i + 1:]:
            start, end = max(first["begin"], second["begin"]), min(first["stop"], second["stop"])
            if start < end:
                spans.append((start, end, sorted({first["probe"], second["probe"]})))
    return spans


def _pair_span(hour_start, spans):
    for start, end, probes in spans:
        if start <= hour_start < end:
            return start, end, probes
    return None


def _within_uncertainty(first, second):
    """两只同层探头在同一柜时的读数是否一致。

    温度、湿度差值都落在合并标准不确定度的 1.96 倍带内才算一致；
    任一指标缺测或超带即视为对不上。
    """
    if first["relative_humidity"] is None or second["relative_humidity"] is None:
        return False, "HUMIDITY"
    checks = []
    for axis, key in (("temp", "celsius"), ("rh", "relative_humidity")):
        value_a, sigma_a = map(Decimal, first[key])
        value_b, sigma_b = map(Decimal, second[key])
        with localcontext() as ctx:
            ctx.prec = 38
            band = Decimal("1.96") * (sigma_a ** 2 + sigma_b ** 2).sqrt()
            checks.append((axis, abs(value_a - value_b) <= band))
    failed = next((axis for axis, ok in checks if not ok), None)
    return failed is None, failed


def adjudicate(snapshot, spec):
    deployments = snapshot["deployments"]
    # 原流水线把重叠在位的测点隔离为 DEPLOYMENT_CONFLICT；裁定要重新核对这些读数。
    # 采信前的格式问题、同刻矛盾、重复回传维持原处置，不进入柜时候选。
    rows = [row for row in snapshot["rows"]
            if row["disposition"] == "accepted"
            or (row["disposition"] == "isolated" and row["code"] == "DEPLOYMENT_CONFLICT")]
    begin, end = seconds(spec.start), seconds(spec.end)
    by_probe = defaultdict(list)
    for row in rows:
        by_probe[row["probe"]].append(row)
    positions = defaultdict(list)
    for window in deployments:
        positions[window["probe"]].append(window)

    def deployed_at(probe, cabinet_id, level, moment):
        return any(window["cabinet"] == cabinet_id and window["level"] == level
                   and window["begin"] <= moment < window["stop"]
                   for window in positions[probe])

    ledger = []
    for cabinet in spec.cabinet_hours:
        upper_spans = _overlap_spans(deployments, cabinet.id, "upper")
        lower_spans = _overlap_spans(deployments, cabinet.id, "lower")
        for hour_start in range(begin, end, HOUR):
            hour_end = hour_start + HOUR
            entry = {"cabinet": cabinet.id, "hour": hour_start, "start": utc(hour_start),
                     "end": utc(hour_end), "primary_probe": None, "primary_row": None,
                     "standby_row": None, "verdict": None, "reason": None, "comparison": None,
                     "overlap_window": None, "candidate_rows": []}
            candidates = [row for probe in by_probe for row in by_probe[probe]
                          if hour_start <= row["measured"] < hour_end
                          and deployed_at(probe, cabinet.id, "upper", row["measured"])]
            candidates.sort(key=lambda row: (row["measured"], row["line"]))
            entry["candidate_rows"] = [row["id"] for row in candidates]
            span = _pair_span(hour_start, upper_spans)
            if span:
                span_start, span_end, pair = span
                entry["overlap_window"] = {"start": utc(span_start), "end": utc(span_end), "probes": pair}
                probes_present = defaultdict(list)
                for row in candidates:
                    probes_present[row["probe"]].append(row)
                entry["comparison"] = {"probes": pair, "present": sorted(probes_present),
                                       "readings": [{"probe": row["probe"], "row": row["id"],
                                                     "measured": row["observed_utc"],
                                                     "temp_c": row["celsius"][0],
                                                     "rh_pct": row["relative_humidity"][0] if row["relative_humidity"] else None}
                                                    for row in candidates]}
                if len(probes_present) == 2:
                    chosen = {}
                    for probe, items in probes_present.items():
                        chosen[probe] = min(items, key=lambda row: (abs(row["measured"] - hour_start - 1800),
                                                                    row["measured"], row["line"]))
                    ordered = [chosen[probe] for probe in pair]
                    agreed, failed_axis = _within_uncertainty(*ordered)
                    entry["comparison"]["agreed"] = agreed
                    entry["comparison"]["failed_axis"] = failed_axis
                    if agreed:
                        primary, standby = ordered
                        entry.update(primary_probe=primary["probe"], primary_row=primary["id"],
                                     standby_row=standby["id"], verdict="ADOPTED",
                                     reason="OVERLAP_AGREED")
                    else:
                        entry.update(verdict="MISSING", reason="OVERLAP_DISPUTED")
                else:
                    entry.update(verdict="MISSING", reason="OVERLAP_UNVERIFIED")
            else:
                if candidates:
                    primary = min(candidates, key=lambda row: (abs(row["measured"] - hour_start - 1800),
                                                               row["measured"], row["line"]))
                    entry.update(primary_probe=primary["probe"], primary_row=primary["id"],
                                 verdict="ADOPTED", reason="SOLE_OUTSIDE_OVERLAP")
                else:
                    entry.update(verdict="MISSING", reason="EMPTY_POSITION")
            entry["note"] = REASONS[entry["reason"]] if entry["reason"] else None
            entry["lower_overlap"] = bool(lower_spans and any(start <= hour_start < end_
                                                              for start, end_, _ in lower_spans))
            ledger.append(entry)
    return ledger
