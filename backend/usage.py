"""Bound model calls in the public demonstration and count provider usage."""

import logging
import os
import time
from contextvars import ContextVar

from fastapi import HTTPException

from .storage import db

reader = ContextVar("model_reader", default=None)
request_id = ContextVar("reading_request_id", default=None)
logger = logging.getLogger("uvicorn.error").getChild("deepseek")


def demo():
    return os.getenv("XUDU_DEMO") == "1"


def charge():
    if not demo():
        return
    day = int(time.time()) // 86400
    limits = [("demo", int(os.getenv("XUDU_DEMO_DAILY_CALLS", "300")))]
    if reader.get():
        limits.append((reader.get(), int(os.getenv("XUDU_DEMO_READER_CALLS", "35"))))
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM model_limits WHERE day<?", (day,))
        for bucket, limit in limits:
            used = c.execute(
                """INSERT INTO model_limits VALUES(?,?,1)
                ON CONFLICT(bucket,day) DO UPDATE SET calls=calls+1 RETURNING calls""",
                (bucket, day),
            ).fetchone()[0]
            if used > limit:
                raise HTTPException(
                    429, "演示今日的模型用量已用完，仍可查阅已保存的回答和引文。"
                )


def record(status, start, payload=None):
    metrics = payload.get("usage") or {} if isinstance(payload, dict) else {}
    logger.info(
        "deepseek request=%s status=%s elapsed_ms=%d input_tokens=%s output_tokens=%s provider=RelayCore",
        request_id.get() or "-",
        status,
        round((time.monotonic() - start) * 1000),
        metrics.get("prompt_tokens"),
        metrics.get("completion_tokens"),
    )


def trace(stage, **fields):
    run = request_id.get()
    if run is not None:
        logger.info("reading request=%s stage=%s %s", run, stage, fields)


def private_feature():
    if demo():
        raise HTTPException(403, "公开演示使用示范书和独立访客书架。")
