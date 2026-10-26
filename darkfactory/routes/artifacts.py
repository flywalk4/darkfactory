"""Project artifacts, versions and task artifact inputs."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import (
    registry,
    service,
)
from ..db import (
    Artifact,
    ArtifactInput,
    ArtifactVersion,
    Session,
    transaction,
)
from ..web import access

router = APIRouter()


class ArtifactVersionInput(BaseModel):
    content: str = Field(min_length=1, max_length=registry.MAX_CONTENT)
    source_task_id: str | None = Field(default=None, max_length=32)


class ArtifactInputModel(ArtifactVersionInput):
    key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    title: str = Field(min_length=1, max_length=300)
    kind: Literal["requirements", "analysis", "architecture", "contract", "report", "document"] = "document"


def project_artifact(db, access, artifact_id):
    artifact = db.get(Artifact, artifact_id)
    if not artifact:
        raise HTTPException(404, "Артефакт не найден")
    access.project(db, artifact.project_id)
    return artifact


@router.get("/api/projects/{project_id}/artifacts")
def project_artifacts(project_id: str, a=Depends(access)):
    with Session() as db:
        a.project(db, project_id)
        return [
            registry.public(db, artifact)
            for artifact in db.scalars(
                select(Artifact).where(Artifact.project_id == project_id).order_by(Artifact.created.desc())
            )
        ]


@router.post("/api/projects/{project_id}/artifacts", status_code=201)
def create_project_artifact(project_id: str, value: ArtifactInputModel, a=Depends(access)):
    a.require("operator")
    if not value.title.strip():
        raise HTTPException(400, "Название не может быть пустым")
    with transaction() as db:
        a.project(db, project_id)
        if db.scalar(select(Artifact).where(Artifact.project_id == project_id, Artifact.key == value.key)):
            raise HTTPException(409, "Артефакт с таким ключом уже существует")
        artifact = Artifact(project_id=project_id, key=value.key, title=value.title.strip(), kind=value.kind)
        db.add(artifact)
        db.flush()
        registry.add_version(db, artifact, value, a)
        return registry.public(db, artifact)


@router.post("/api/project-artifacts/{artifact_id}/versions", status_code=201)
def create_artifact_version(artifact_id: str, value: ArtifactVersionInput, a=Depends(access)):
    a.require("operator")
    with transaction() as db:
        artifact = project_artifact(db, a, artifact_id)
        version = registry.add_version(db, artifact, value, a)
        return registry.version_public(db, artifact, version, content=True)


@router.get("/api/project-artifacts/{artifact_id}/versions/{version_id}")
def get_artifact_version(artifact_id: str, version_id: str, a=Depends(access)):
    with Session() as db:
        artifact = project_artifact(db, a, artifact_id)
        version = db.get(ArtifactVersion, version_id)
        if not version or version.artifact_id != artifact.id:
            raise HTTPException(404, "Версия не найдена")
        return registry.version_public(db, artifact, version, content=True)


@router.post("/api/project-artifacts/{artifact_id}/versions/{version_id}/accept")
def accept_artifact_version(artifact_id: str, version_id: str, a=Depends(access)):
    a.require("owner")
    with transaction() as db:
        artifact = project_artifact(db, a, artifact_id)
        version = db.get(ArtifactVersion, version_id)
        if not version:
            raise HTTPException(404, "Версия не найдена")
        registry.accept(db, artifact, version, a)
        registry.refresh_consumers(db, artifact)
        return registry.public(db, artifact)


@router.post("/api/project-artifacts/{artifact_id}/revoke")
def revoke_artifact(artifact_id: str, a=Depends(access)):
    a.require("owner")
    with transaction() as db:
        artifact = project_artifact(db, a, artifact_id)
        artifact.accepted_version_id = None
        registry.refresh_consumers(db, artifact)
        return registry.public(db, artifact)


@router.get("/api/tasks/{task_id}/artifact-inputs")
def task_artifact_inputs(task_id: str, a=Depends(access)):
    with Session() as db:
        task = a.task(db, task_id)
        return [
            {
                "artifact_id": item.artifact_id,
                "key": db.get(Artifact, item.artifact_id).key,
                "title": db.get(Artifact, item.artifact_id).title,
                **registry.version_public(
                    db, db.get(Artifact, item.artifact_id), db.get(ArtifactVersion, item.version_id)
                ),
                "blocked_reason": registry.input_reason(db, task),
            }
            for item in registry.inputs(db, task)
        ]


class ArtifactBindingInput(BaseModel):
    version_id: str = Field(min_length=1, max_length=32)


@router.put("/api/tasks/{task_id}/artifact-inputs/{artifact_id}")
def bind_task_artifact(task_id: str, artifact_id: str, value: ArtifactBindingInput, a=Depends(access)):
    a.require("operator")
    with transaction() as db:
        task = a.task(db, task_id)
        artifact = project_artifact(db, a, artifact_id)
        version = db.get(ArtifactVersion, value.version_id)
        if not version:
            raise HTTPException(404, "Версия не найдена")
        registry.bind(db, task, artifact, version)
    return {"ok": True}


@router.delete("/api/tasks/{task_id}/artifact-inputs/{artifact_id}")
def unbind_task_artifact(task_id: str, artifact_id: str, a=Depends(access)):
    a.require("operator")
    with transaction() as db:
        task = a.task(db, task_id)
        if task.state not in service.READY | {"needs_human"}:
            raise HTTPException(409, "Сначала останови задачу")
        item = db.scalar(
            select(ArtifactInput).where(
                ArtifactInput.task_id == task.id, ArtifactInput.artifact_id == artifact_id
            )
        )
        if not item:
            raise HTTPException(404, "Входной артефакт не найден")
        db.delete(item)
        db.flush()
        registry.changed(db, task, "Входной артефакт удалён")
    return {"ok": True}
