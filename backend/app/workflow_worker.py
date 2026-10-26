"""Run with ``python -m app.workflow_worker`` after initializing the API schema."""

import argparse
import asyncio
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import or_, select, text, update

from app.database import async_session_maker, engine
from app.models.workflow import WorkflowRun
from app.services.workflow_service import ACTIVE_STATES, process_run

logger = logging.getLogger(__name__)
WORKER_LOCK = 684731052


async def _heartbeat(run_id, worker_id, stop, lock_connection):
    while not stop.is_set():
        # The advisory lock lives on this connection. A lost connection cancels work.
        await lock_connection.execute(text("SELECT 1"))
        async with async_session_maker() as db:
            await db.execute(update(WorkflowRun).where(
                WorkflowRun.id == run_id, WorkflowRun.lease_owner == worker_id,
            ).values(lease_until=datetime.utcnow() + timedelta(seconds=90)))
            await db.commit()
        try:
            await asyncio.wait_for(stop.wait(), timeout=20)
        except asyncio.TimeoutError:
            pass


async def _run_one(db, run, worker_id, connection):
    run_id = run.id
    run.lease_owner, run.lease_until = worker_id, datetime.utcnow() + timedelta(seconds=90)
    await db.commit()
    stop = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(run_id, worker_id, stop, connection))
    task = asyncio.create_task(process_run(db, run))
    try:
        done, _ = await asyncio.wait({heartbeat, task}, return_when=asyncio.FIRST_COMPLETED)
        if heartbeat in done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await heartbeat
        await task
    finally:
        stop.set()
        await asyncio.gather(heartbeat, return_exceptions=True)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await db.rollback()
        await db.execute(update(WorkflowRun).where(WorkflowRun.id == run_id, WorkflowRun.lease_owner == worker_id).values(lease_owner=None, lease_until=None))
        await db.commit()


async def run_worker(once=False):
    if engine.dialect.name != "postgresql":
        raise RuntimeError("持久工作流 worker 需要 PostgreSQL advisory lock")
    worker_id = str(uuid.uuid4())
    async with engine.connect() as connection:
        locked = (await connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK})).scalar()
        if not locked:
            raise RuntimeError("已有工作流 worker 正在运行")
        try:
            while True:
                async with async_session_maker() as db:
                    now = datetime.utcnow()
                    run = (await db.execute(select(WorkflowRun).where(
                        WorkflowRun.state.in_(ACTIVE_STATES),
                        or_(WorkflowRun.next_attempt_at.is_(None), WorkflowRun.next_attempt_at <= now),
                    ).order_by(WorkflowRun.created_at, WorkflowRun.id).limit(1))).scalar_one_or_none()
                    if run:
                        await _run_one(db, run, worker_id, connection)
                if once:
                    return
                if not run:
                    await asyncio.sleep(2)
        finally:
            if not connection.invalidated:
                await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="发票工作流持久任务处理器")
    parser.add_argument("--once", action="store_true", help="处理最多一个待执行批次后退出")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker(once=args.once))
