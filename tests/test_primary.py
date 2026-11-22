import pytest
from concurrent.futures import ThreadPoolExecutor

from app.manifest import json_sha
from app.modules.archive.pipeline import evaluate
from app.modules.intake.catalog import load_sample
from app.modules.primary.adjudicate import adjudicate
from app.modules.primary.reevaluate import reevaluate
from app.modules.primary.service import ADJUDICATION_POLICY, PrimaryService, batch_key
from app.timebase import seconds


@pytest.fixture(scope="session")
def swap_package():
    return load_sample("swap")


@pytest.fixture(scope="session")
def swap_snapshot(swap_package):
    return evaluate(swap_package)


@pytest.fixture(scope="session")
def swap_ledger(swap_package, swap_snapshot):
    return adjudicate(swap_snapshot, swap_package.spec)


def reason_at(ledger, iso_hour):
    hour = seconds(f"2026-11-20T{iso_hour}:00+08:00")
    return next(entry for entry in ledger if entry["hour"] == hour)


def test_overlap_window_kept_for_each_disputed_hour(swap_ledger):
    # 重叠窗 11:00-14:00（北京）逐小时挂账，不整段判一只
    overlap = [entry for entry in swap_ledger if entry["overlap_window"]]
    assert [entry["start"] for entry in overlap] == [
        "2026-11-20T03:00:00Z", "2026-11-20T04:00:00Z", "2026-11-20T05:00:00Z"]
    assert all(entry["overlap_window"]["probes"] == ["0001", "0031"] for entry in overlap)


def test_agreed_overlap_adopts_with_comparison_basis(swap_ledger):
    entry = reason_at(swap_ledger, "11")
    assert (entry["verdict"], entry["reason"]) == ("ADOPTED", "OVERLAP_AGREED")
    assert entry["primary_probe"] == "0001" and entry["standby_row"] is not None
    assert entry["comparison"]["agreed"] is True
    # 采用的是更靠近小时中点的读数，原行可追溯
    assert entry["primary_row"].endswith(":8")


def test_disputed_overlap_hour_stays_missing_without_picking_a_probe(swap_ledger):
    # 两只都有读数但温差 2.2C 超出 1.96 倍合并不确定度：保留缺证，不按错的那只凑账
    entry = reason_at(swap_ledger, "12")
    assert (entry["verdict"], entry["reason"]) == ("MISSING", "OVERLAP_DISPUTED")
    assert entry["primary_probe"] is None and entry["primary_row"] is None
    assert entry["comparison"]["agreed"] is False
    assert entry["comparison"]["failed_axis"] == "temp"


def test_single_reading_overlap_hour_is_unverified_missing(swap_ledger):
    # 重叠期只有旧探头一只读数：缺比对证据，保留缺证
    entry = reason_at(swap_ledger, "13")
    assert (entry["verdict"], entry["reason"]) == ("MISSING", "OVERLAP_UNVERIFIED")
    assert entry["comparison"]["present"] == ["0001"]
    assert entry["primary_probe"] is None


def test_non_overlap_hours_follow_the_only_deployed_probe(swap_ledger):
    before = reason_at(swap_ledger, "09")
    after = reason_at(swap_ledger, "15")
    assert (before["primary_probe"], before["reason"]) == ("0001", "SOLE_OUTSIDE_OVERLAP")
    assert (after["primary_probe"], after["reason"]) == ("0031", "SOLE_OUTSIDE_OVERLAP")


def test_intake_quarantined_rows_never_become_candidates(swap_ledger):
    # 时钟倒置、空温度、未登记探头、班次外读数不进入任何柜时候选
    for entry in swap_ledger:
        joined = " ".join(entry["candidate_rows"])
        for blocked in (":25", ":27", ":28", ":29"):
            assert blocked not in joined


def test_reevaluated_grades_keep_missing_hours_and_segment_episodes(swap_package, swap_ledger, swap_snapshot):
    result = reevaluate(swap_package.spec, swap_ledger, swap_snapshot)
    states = [(g["start"][11:13], g["state"], g["adjudication_reason"]) for g in result["grades"]]
    assert ("04", "MISSING", "OVERLAP_DISPUTED") in states
    assert ("05", "MISSING", "OVERLAP_UNVERIFIED") in states
    # 缺证切断持续异常：换装前后各成一段，不跨缺证小时合并
    episodes = {(item["id"], item["kind"], item["hours"]) for item in result["episodes"]}
    assert episodes == {("CV-17-A01", "SUSTAINED", 2), ("CV-17-A02", "SUSTAINED", 2)}
    assert result["hour_tally"] == {"expected": 10, "stable": 4, "abnormal": 4, "missing": 2}


def test_ticket_handoff_marks_alarms_on_missing_hours(swap_package, swap_ledger, swap_snapshot):
    handoff = reevaluate(swap_package.spec, swap_ledger, swap_snapshot)["handoff"]
    codes = {ticket["id"]: ticket["code"] for ticket in handoff["tickets"]}
    assert codes["AL-0801"] == "MATCHED" and codes["AL-0804"] == "MATCHED"
    # 落在分歧/缺比对小时的报警不核销，单列证据不足
    assert codes["AL-0802"] == "MISSING_EVIDENCE" and codes["AL-0803"] == "MISSING_EVIDENCE"
    assert codes["AL-0805"] == "STABLE_AT_ALARM" and codes["AL-0806"] == "UNKNOWN_CABINET"
    assert handoff["unreported_episodes"] == []
    assert handoff["missing_hours"] == ["2026-11-20T04:00:00Z", "2026-11-20T05:00:00Z"]


def test_agreement_band_uses_combined_uncertainty(swap_snapshot):
    rows = {row["id"]: row for row in swap_snapshot["rows"]}
    from app.modules.primary.adjudicate import _within_uncertainty
    first, second = rows["swap-points.csv:8"], rows["swap-points.csv:9"]
    assert _within_uncertainty(first, second) == (True, None)
    warm, cool = rows["swap-points.csv:11"], rows["swap-points.csv:12"]
    assert _within_uncertainty(warm, cool) == (False, "temp")


# ---------- HTTP / 版本化 ----------

@pytest.fixture
def swap_run(client):
    reply = client.post("/api/runs", json={"sample": "swap"})
    assert reply.status_code == 201
    return reply.json()["manifest"]["id"]


@pytest.fixture
def primary_base(swap_run):
    return f"/api/runs/{swap_run}/primary"


def test_conclusion_requires_explicit_issue(client, primary_base):
    assert client.get(primary_base).status_code == 404
    reply = client.post(primary_base)
    assert reply.status_code == 201 and reply.json()["reused"] is False
    assert reply.json()["conclusion"]["version"] == 1


def test_same_batch_rerun_returns_same_version(client, primary_base):
    first = client.post(primary_base).json()["conclusion"]
    again = client.post(primary_base)
    assert again.status_code == 200 and again.json()["reused"] is True
    assert again.json()["conclusion"]["id"] == first["id"]
    assert [d["version"] for d in client.get(primary_base + "/versions").json()["versions"]] == [1]
    assert client.get(primary_base).json()["digest"] == first["digest"]
    assert client.get(primary_base + "/versions/1").json()["id"] == first["id"]
    assert client.get(primary_base + "/versions/9").status_code == 404


def test_concurrent_same_batch_submission_persists_single_version(client, swap_run):
    from app.modules.primary.service import PrimaryService
    service = PrimaryService(client.app.state.archives.store)
    with ThreadPoolExecutor(max_workers=5) as pool:
        outcomes = list(pool.map(lambda _: service.build(swap_run), range(5)))
    assert sum(created for _, created in outcomes) == 1
    assert len({doc["id"] for doc, _ in outcomes}) == 1
    assert [d["version"] for d in service.versions(swap_run)] == [1]


def test_batch_key_is_pinned_to_archive_and_adjudication_policy(client, swap_run):
    manifest = client.get(f"/api/runs/{swap_run}").json()
    assert batch_key(manifest) == client.post(f"/api/runs/{swap_run}/primary").json()["conclusion"]["batch"]
    changed = {**manifest, "policy": "glass-9.9.9"}
    assert batch_key(changed) != batch_key(manifest)
    assert ADJUDICATION_POLICY.startswith("primary-adjudication")


def test_write_failure_rolls_back_and_keeps_old_version(client, primary_base):
    first = client.post(primary_base).json()["conclusion"]
    store = client.app.state.archives.store
    with store.connect() as db:
        db.execute("CREATE TRIGGER primary_fail AFTER INSERT ON primary_conclusions "
                   "BEGIN SELECT RAISE(ABORT, 'disk'); END")
    other = client.post("/api/runs", json={"sample": "clean"}).json()["manifest"]["id"]
    failed = client.post(f"/api/runs/{other}/primary")
    assert failed.status_code == 503 and failed.json()["code"] == "ARCHIVE_UNAVAILABLE"
    with store.connect() as db:
        db.execute("DROP TRIGGER primary_fail")
    # 旧版结论仍可读，失败未产生空版本；恢复后新归卷正常出 v1
    assert client.get(primary_base).json()["version"] == 1
    recovered = client.post(f"/api/runs/{other}/primary")
    assert recovered.status_code == 201 and recovered.json()["conclusion"]["version"] == 1
    assert client.get(primary_base).json()["id"] == first["id"]


def test_conclusion_digest_is_canonical_and_covers_ledger(client, primary_base):
    doc = client.post(primary_base).json()["conclusion"]
    assert doc["digest"] == json_sha({key: value for key, value in doc.items()
                                      if key not in {"id", "version", "created", "digest", "basis"}})
    assert doc["basis"]["run_result_sha256"]


def test_primary_service_rejects_unknown_run(client):
    service = PrimaryService(client.app.state.archives.store)
    with pytest.raises(Exception):
        service.build("not-a-run")


def test_shift_sample_without_overlap_adjudicates_all_sole_hours(client):
    run = client.post("/api/runs", json={"sample": "shift"}).json()["manifest"]["id"]
    doc = client.post(f"/api/runs/{run}/primary").json()["conclusion"]
    assert set(doc["reason_tally"]) <= {"SOLE_OUTSIDE_OVERLAP", "EMPTY_POSITION"}
    assert doc["hour_tally"]["expected"] == 20
