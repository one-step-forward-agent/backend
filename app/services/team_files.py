"""Team attachment access always follows current membership and task-management rights."""
import asyncio
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.lang import tr
from app.models.models import TeamTask, TeamTaskFile, User
from app.services import teams

ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "png", "jpg", "jpeg"}


async def task_access(session: AsyncSession, user: User, team_id: int, task_id: int, manage: bool = False) -> TeamTask:
    _, member = await teams.member_of(session, user, team_id)
    task = await session.get(TeamTask, task_id)
    if not task or task.team_id != team_id:
        raise LookupError(tr("Задача не найдена", "Task not found"))
    if manage and task.created_by != user.id and member.role != "admin":
        raise PermissionError(tr("Файлы меняет автор задачи или администратор", "Only the task creator or an admin can change attachments"))
    return task


def file_path(record: TeamTaskFile) -> Path:
    # Only generated basenames are stored; supplied filenames never become paths.
    return settings.storage_path / "team-tasks" / str(record.task_id) / record.stored_filename


def view(record: TeamTaskFile) -> dict:
    return {"id": record.id, "filename": record.original_filename, "size": record.file_size, "mime_type": record.mime_type}


async def add(session: AsyncSession, user: User, team_id: int, task_id: int, filename: str, mime_type: str, content: bytes) -> dict:
    await task_access(session, user, team_id, task_id, manage=True)
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix not in ALLOWED_EXTENSIONS:
        raise teams.TeamError(tr("Неподдерживаемый тип файла", "Unsupported file type"))
    record = TeamTaskFile(task_id=task_id, original_filename=filename[:255], stored_filename=f"{uuid4().hex}.{suffix}", mime_type=mime_type[:100], file_size=len(content))
    target = file_path(record)
    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(target.write_bytes, content)
    try:
        session.add(record)
        await session.commit()
    except Exception:
        await asyncio.to_thread(target.unlink, missing_ok=True)
        raise
    await session.refresh(record)
    return view(record)


async def listing(session: AsyncSession, user: User, team_id: int, task_id: int) -> list[dict]:
    await task_access(session, user, team_id, task_id)
    return [view(record) for record in await session.scalars(select(TeamTaskFile).where(TeamTaskFile.task_id == task_id).order_by(TeamTaskFile.id))]


async def get(session: AsyncSession, user: User, team_id: int, task_id: int, file_id: int, manage: bool = False) -> TeamTaskFile:
    await task_access(session, user, team_id, task_id, manage)
    record = await session.get(TeamTaskFile, file_id)
    if not record or record.task_id != task_id:
        raise LookupError(tr("Файл не найден", "File not found"))
    return record


async def remove(session: AsyncSession, user: User, team_id: int, task_id: int, file_id: int) -> None:
    record = await get(session, user, team_id, task_id, file_id, manage=True)
    await asyncio.to_thread(file_path(record).unlink, missing_ok=True)
    await session.delete(record)
    await session.commit()


async def remove_task_contents(session: AsyncSession, task_ids: list[int]) -> None:
    from app.services import completion
    await completion.cleanup(session, task_ids=task_ids)
    for record in await session.scalars(select(TeamTaskFile).where(TeamTaskFile.task_id.in_(task_ids))):
        await asyncio.to_thread(file_path(record).unlink, missing_ok=True)
