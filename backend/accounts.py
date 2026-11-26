"""Private accounts and independently revocable browser sessions."""

import hashlib
import os
import re
import secrets
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, Field

GUEST_COOKIE = "xudu_session"
ACCOUNT_COOKIE = "xudu_account"
SESSION_AGE = 30 * 86400


@dataclass(frozen=True)
class ReaderIdentity:
    owner: str
    session: str
    epoch: int = 0
    username: str | None = None


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)
    invitation: str = Field(default="", max_length=160)


def token():
    return secrets.token_urlsafe(32)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def identity(connection, request: Request):
    account = request.cookies.get(ACCOUNT_COOKIE)
    if account:
        session = digest(account)
        row = connection.execute(
            """SELECT a.id,a.username,s.epoch FROM account_sessions s
               JOIN accounts a ON a.id=s.account WHERE s.token=? AND s.expires>?""",
            (session, int(time.time())),
        ).fetchone()
        if row:
            return ReaderIdentity(row["id"], session, row["epoch"], row["username"])
    visitor = request.cookies.get(GUEST_COOKIE, "")
    if connection.execute("SELECT 1 FROM visitors WHERE id=?", (visitor,)).fetchone():
        return ReaderIdentity(visitor, visitor)
    return None


def session_state(connection, reader, *, same_epoch=False):
    if reader.username:
        row = connection.execute(
            "SELECT active,epoch FROM account_sessions WHERE token=? AND account=? AND expires>?",
            (reader.session, reader.owner, int(time.time())),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT active,0 AS epoch FROM visitors WHERE id=?", (reader.owner,)
        ).fetchone()
    if not row:
        raise HTTPException(401, "登录已结束，请重新登录。")
    if same_epoch and row["epoch"] != reader.epoch:
        raise HTTPException(409, "本浏览器已切换书籍，请按当前位置重试。")
    return row


def cookie(response: Response, name, value, age):
    response.set_cookie(
        name,
        value,
        max_age=age,
        httponly=True,
        samesite="strict",
        secure=os.getenv("XUDU_SECURE_COOKIES") == "1",
    )


def password_digest(password, salt):
    return hashlib.scrypt(
        password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=64 * 1024**2
    )


def normalized_name(username):
    return username.strip().casefold()


def validate_registration(data):
    username = data.username.strip()
    if not re.fullmatch(r"[\w.-]{3,32}", username):
        raise HTTPException(
            422, "用户名为 3–32 个字，可用文字、数字、下划线、点和连字符。"
        )
    if len(data.password) < 10:
        raise HTTPException(422, "密码至少需要 10 个字符。")
    configured = os.getenv("XUDU_INVITATION", "")
    if configured and not secrets.compare_digest(
        data.invitation.encode(), configured.encode()
    ):
        raise HTTPException(403, "邀请码不正确。")
    return username


def start_session(connection, account, active, response):
    secret = token()
    now = int(time.time())
    connection.execute("DELETE FROM account_sessions WHERE expires<=?", (now,))
    connection.execute(
        "INSERT INTO account_sessions(token,account,active,expires) VALUES(?,?,?,?)",
        (digest(secret), account, active, now + SESSION_AGE),
    )
    cookie(response, ACCOUNT_COOKIE, secret, SESSION_AGE)


def throttle(connection, request, operation):
    now = int(time.time())
    address = request.client.host if request.client else "local"
    key = digest(operation + "|" + address)
    connection.execute("DELETE FROM login_limits WHERE until<=?", (now,))
    saved = connection.execute(
        "SELECT attempts FROM login_limits WHERE key=?", (key,)
    ).fetchone()
    if saved and saved[0] >= 20:
        raise HTTPException(429, "操作过于频繁，请稍后再试。")
    connection.execute(
        """INSERT INTO login_limits VALUES(?,?,1) ON CONFLICT(key)
           DO UPDATE SET attempts=attempts+1""",
        (key, now + 900),
    )
