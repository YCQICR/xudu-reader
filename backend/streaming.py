"""Stream verified results and short progress updates to the reading workspace."""

import asyncio
import json
import logging
from contextlib import suppress
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from . import usage

# A local workbench runs one app process. Entries live only for the duration of a request.
running = {}


async def deliver(request, who, request_id, action):
    async def traced(report):
        token = usage.request_id.set(str(request_id or uuid4()))
        usage.trace("start")
        try:
            return await action(report)
        except HTTPException as exc:
            usage.trace("request_error", status=exc.status_code)
            raise
        except asyncio.CancelledError:
            usage.trace("cancelled")
            raise
        finally:
            usage.trace("finished")
            usage.request_id.reset(token)

    if "application/x-ndjson" not in request.headers.get("accept", ""):
        return await traced(lambda _: None)
    if request_id is None:
        raise HTTPException(422, "缺少本次请求编号。")
    key = (who, request_id)
    if key in running:
        raise HTTPException(409, "这个问题正在处理。")
    queue = asyncio.Queue()

    def report(message):
        queue.put_nowait({"status": message})

    def finished(task):
        if task.cancelled():
            queue.put_nowait({"error": "已停止本次回答。"})
            return
        try:
            queue.put_nowait({"result": task.result()})
        except HTTPException as exc:
            queue.put_nowait({"error": exc.detail})
        except Exception:
            logging.getLogger(__name__).exception("Reading request failed")
            queue.put_nowait({"error": "处理未完成，可以重试。"})

    task = asyncio.create_task(traced(report))
    task.add_done_callback(finished)
    running[key] = task

    async def events():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 1)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield "\n"
                    continue
                yield json.dumps(event, ensure_ascii=False) + "\n"
                if "status" not in event:
                    break
        finally:
            running.pop(key, None)
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    return StreamingResponse(
        events(), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no"}
    )
