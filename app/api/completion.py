"""The report is optional; saving it never changes completion status."""
import asyncio

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.teams import answer
from app.core.config import settings
from app.core.database import get_session
from app.core.lang import tr
from app.models.models import User
from app.schemas import CompletionReportWrite
from app.services import completion

router = APIRouter(prefix="/api", tags=["completion"])


@router.get("/events/{event_id}/completion-report")
async def event_report(event_id: int, response: Response, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    response.headers["Cache-Control"] = "no-store"
    return await answer(completion.read(session, user, event_id=event_id))


@router.put("/events/{event_id}/completion-report")
async def save_event_report(event_id: int, payload: CompletionReportWrite, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(completion.save(session, user, payload.model_dump(), event_id=event_id))


@router.get("/teams/{team_id}/tasks/{task_id}/completion-report")
async def team_report(team_id: int, task_id: int, response: Response, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    response.headers["Cache-Control"] = "no-store"
    return await answer(completion.read(session, user, team_id=team_id, task_id=task_id))


@router.put("/teams/{team_id}/tasks/{task_id}/completion-report")
async def save_team_report(team_id: int, task_id: int, payload: CompletionReportWrite, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await answer(completion.save(session, user, payload.model_dump(), team_id=team_id, task_id=task_id))


async def upload_report_file(session, user, file, **scope):
    from app.api.routes import limit_uploads

    await answer(completion.target(session, user, write=True, **scope))
    limit_uploads(user)
    content = await file.read(settings.max_file_size_mb * 1024 * 1024 + 1)
    if len(content) > settings.max_file_size_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=tr("Файл слишком большой", "File is too large"))
    return await answer(completion.upload(session, user, file.filename or "", content, **scope))


@router.post("/events/{event_id}/completion-report/files", status_code=201)
async def event_file(event_id: int, file: UploadFile = File(...), user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await upload_report_file(session, user, file, event_id=event_id)


@router.post("/teams/{team_id}/tasks/{task_id}/completion-report/files", status_code=201)
async def team_file(team_id: int, task_id: int, file: UploadFile = File(...), user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    return await upload_report_file(session, user, file, team_id=team_id, task_id=task_id)


@router.get("/completion-files/{file_id}")
async def download_file(file_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    record = await answer(completion.owned_file(session, user, file_id))
    path = completion.file_path(record)
    if not await asyncio.to_thread(path.is_file):
        raise HTTPException(status_code=404, detail=tr("Файл не найден", "File not found"))
    return FileResponse(path, filename=record.original_filename, media_type="application/octet-stream", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.delete("/completion-files/{file_id}", status_code=204)
async def delete_file(file_id: int, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    await answer(completion.remove_file(session, user, file_id))
