"""Completion evidence belongs only to Team Development tasks and current team members."""
import asyncio
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.lang import tr
from app.models.models import CompletionFile, CompletionReport, Event, TeamTask, User
from app.services import teams

MAX_FILES = 20
ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "xls", "xlsx", "png", "jpg", "jpeg"}


async def target(session: AsyncSession, user: User, *, event_id: int | None = None, team_id: int | None = None, task_id: int | None = None, write: bool = False):
    event = None
    if event_id is not None:
        event = await session.get(Event, event_id)
        if not event or event.user_id != user.id:
            raise LookupError(tr("Задача не найдена", "Task not found"))
        task = await session.scalar(select(TeamTask).where(TeamTask.event_id == event.id))
        if not task:
            raise LookupError(tr("Отчёт доступен только для командных задач", "Reports are only available for team tasks"))
        team_id, task_id = task.team_id, task.id
    if task_id is not None:
        _, member = await teams.member_of(session, user, team_id)
        task = await session.get(TeamTask, task_id, with_for_update=write)
        if not task or task.team_id != team_id:
            raise LookupError(tr("Задача не найдена", "Task not found"))
        can_edit = task.assignee_id == member.id or (task.assignee_id is None and (member.role == "admin" or task.created_by == user.id))
        completed = task.status == "done"
        condition = CompletionReport.team_task_id == task.id
        values = {"team_task_id": task.id}
    else:
        raise LookupError(tr("Задача не найдена", "Task not found"))
    if write and not can_edit:
        raise PermissionError(tr("Отчёт заполняет исполнитель задачи", "Only the task performer can edit the report"))
    if write and not completed:
        raise teams.TeamError(tr("Сначала отметьте задачу выполненной", "Mark the task as done first"))
    report = await session.scalar(select(CompletionReport).where(condition).with_for_update() if write else select(CompletionReport).where(condition))
    return report, values, can_edit and completed


async def view(session: AsyncSession, report: CompletionReport | None, can_edit: bool) -> dict:
    files = list(await session.scalars(select(CompletionFile).where(CompletionFile.report_id == report.id).order_by(CompletionFile.id))) if report else []
    return {
        "tools": (report.details or {}).get("tools", "") if report else "",
        "conclusions": (report.details or {}).get("conclusions", "") if report else "",
        "note": (report.details or {}).get("note", "") if report else "",
        "updated_at": report.updated_at.isoformat() if report else None,
        "can_edit": can_edit,
        "files": [{"id": file.id, "filename": file.original_filename, "size": file.file_size} for file in files],
    }


async def read(session: AsyncSession, user: User, **scope) -> dict:
    report, _, can_edit = await target(session, user, **scope)
    return await view(session, report, can_edit)


async def save(session: AsyncSession, user: User, details: dict, **scope) -> dict:
    report, values, can_edit = await target(session, user, write=True, **scope)
    if report is None:
        report = CompletionReport(**values, details=details)
        session.add(report)
    else:
        report.details = details
    await session.commit()
    await session.refresh(report)
    return await view(session, report, can_edit)


def file_path(record: CompletionFile) -> Path:
    return settings.storage_path / "completion" / str(record.report_id) / record.stored_filename


async def upload(session: AsyncSession, user: User, filename: str, content: bytes, **scope) -> dict:
    report, values, can_edit = await target(session, user, write=True, **scope)
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix not in ALLOWED_EXTENSIONS:
        raise teams.TeamError(tr("Неподдерживаемый тип файла", "Unsupported file type"))
    if len(content) > settings.max_file_size_mb * 1024 * 1024:
        raise teams.TeamError(tr("Файл слишком большой", "File is too large"))
    if report is None:
        report = CompletionReport(**values, details={})
        session.add(report)
        await session.flush()
    count = await session.scalar(select(func.count()).select_from(CompletionFile).where(CompletionFile.report_id == report.id))
    if count >= MAX_FILES:
        raise teams.TeamError(tr("К отчёту можно прикрепить не больше 20 файлов", "A report can have at most 20 files"))
    record = CompletionFile(report_id=report.id, original_filename=filename.replace("\\", "/").rsplit("/", 1)[-1][:255], stored_filename=f"{uuid4().hex}.{suffix}", file_size=len(content))
    path = file_path(record)
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    try:
        await asyncio.to_thread(path.write_bytes, content)
        session.add(record)
        await session.commit()
    except Exception:
        await session.rollback()
        await asyncio.to_thread(path.unlink, missing_ok=True)
        raise
    await session.refresh(report)
    return await view(session, report, can_edit)


async def owned_file(session: AsyncSession, user: User, file_id: int, write: bool = False) -> CompletionFile:
    record = await session.get(CompletionFile, file_id)
    report = await session.get(CompletionReport, record.report_id) if record else None
    if report is None:
        raise LookupError(tr("Файл не найден", "File not found"))
    task = await session.get(TeamTask, report.team_task_id) if report.team_task_id else None
    await target(session, user, event_id=report.event_id, team_id=task.team_id if task else None, task_id=report.team_task_id, write=write)
    return record


async def remove_file(session: AsyncSession, user: User, file_id: int) -> None:
    record = await owned_file(session, user, file_id, write=True)
    await asyncio.to_thread(file_path(record).unlink, missing_ok=True)
    await session.delete(record)
    await session.commit()


async def cleanup(session: AsyncSession, *, event_ids: list[int] = (), task_ids: list[int] = ()) -> None:
    from sqlalchemy import or_

    reports = select(CompletionReport.id).where(or_(CompletionReport.event_id.in_(event_ids), CompletionReport.team_task_id.in_(task_ids)))
    for record in await session.scalars(select(CompletionFile).where(CompletionFile.report_id.in_(reports))):
        await asyncio.to_thread(file_path(record).unlink, missing_ok=True)
