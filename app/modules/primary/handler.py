from fastapi import APIRouter, Response

from app.dependencies import Archive, Service
from app.errors import FieldError
from app.modules.primary.service import PrimaryService

router = APIRouter(prefix="/api/runs/{run_id}/primary")


def primary(service: Service) -> PrimaryService:
    return PrimaryService(service.store)


@router.post("", status_code=201)
def conclude(run_id: str, service: Service, response: Response):
    doc, created = primary(service).build(run_id)
    if not created:
        response.status_code = 200
    return {"conclusion": doc, "reused": not created}


@router.get("")
def latest(archive: Archive, service: Service):
    return primary(service).latest(archive[0]["id"])


@router.get("/versions")
def versions(archive: Archive, service: Service):
    return {"versions": primary(service).versions(archive[0]["id"])}


@router.get("/versions/{version}")
def version(archive: Archive, service: Service, version: int):
    docs = primary(service).versions(archive[0]["id"])
    for doc in docs:
        if doc["version"] == version:
            return doc
    raise FieldError("UNKNOWN_VERSION", "主探头版次未登记", 404)
