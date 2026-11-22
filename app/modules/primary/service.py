"""主探头结论的版本化服务。

归卷（shift_archives）固定原档与规则摘要；主探头结论是在固定归卷之上的裁定版次：
- 同一换装批次（由归卷号、政策、裁定算法版次决定）重跑不新增版本；
- 写入失败由存储事务回滚，旧版结论保持可读；
- 归卷沿用同一版主探头，后续重跑复算的是同一批次。
"""
import sqlite3
from datetime import datetime, timezone

from app.config import POLICY
from app.errors import FieldError
from app.manifest import json_sha
from app.modules.intake.catalog import restore
from app.modules.primary.adjudicate import adjudicate
from app.modules.primary.reevaluate import reevaluate

ADJUDICATION_POLICY = "primary-adjudication-1.0.0"


def batch_key(manifest):
    return json_sha([manifest["id"], manifest["policy"], ADJUDICATION_POLICY])[:16]


class PrimaryService:
    def __init__(self, store):
        self.store = store

    def _spec(self, archive):
        return restore(archive[2]).spec

    def build(self, run_id):
        archive = self.store.fetch(run_id)
        manifest, snapshot, _ = archive
        spec = self._spec(archive)
        batch = batch_key(manifest)
        existing = self.store.conclusion_version(run_id, batch)
        if existing is not None:
            return existing, False
        ledger = adjudicate(snapshot, spec)
        result = reevaluate(spec, ledger, snapshot)
        body = {"run_id": run_id, "batch": batch, "policy": POLICY,
                "adjudication_policy": ADJUDICATION_POLICY,
                "shift": manifest["shift"], "window": manifest["window"],
                "cabinets": [item["id"] for item in manifest["cabinets"]],
                "position_conflicts": snapshot["deployment_conflicts"],
                "ledger": ledger, **result}
        digest = json_sha(body)
        version = len(self.store.conclusion_versions(run_id)) + 1
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        doc_id = json_sha([run_id, batch, version])[:24]
        doc = {**body, "id": doc_id, "version": version, "created": created,
               "digest": digest, "basis": {"run_result_sha256": manifest["result_sha256"],
                                           "file_sha256": manifest["hashes"]["file_sha256"],
                                           "contract_sha256": manifest["hashes"]["contract_sha256"]}}
        try:
            saved, created_flag = self.store.save_conclusion(doc, digest, created)
        except sqlite3.IntegrityError:
            # 并发下另一线程已为同一批次落版：复用已存版，不新增版本。
            existing = self.store.conclusion_version(run_id, batch)
            if existing is None:
                raise
            return existing, False
        if saved.get("digest") != digest:
            raise FieldError("PRIMARY_DRIFT", "主探头结论摘要不一致", 409)
        return saved, created_flag

    def latest(self, run_id):
        self.store.fetch(run_id)
        doc = self.store.latest_conclusion(run_id)
        if doc is None:
            raise FieldError("NO_PRIMARY_CONCLUSION", "该归卷尚未出具主探头结论", 404)
        return doc

    def versions(self, run_id):
        self.store.fetch(run_id)
        return self.store.conclusion_versions(run_id)
