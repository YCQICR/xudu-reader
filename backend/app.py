import asyncio
import hashlib
import json
import os
import sqlite3
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import pymupdf
from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import accounts, agent, streaming, tracking, topics, usage
from .importer import import_pdf
from .storage import DATA, ROOT, SAMPLE, SHOWCASE, db, decode, setup

SAMPLE_DIGEST = hashlib.sha256(SAMPLE.read_bytes()).hexdigest()
SHOWCASE_DIGEST = hashlib.sha256(SHOWCASE.read_bytes()).hexdigest()


def uid():
    return secrets.token_urlsafe(20)


@asynccontextmanager
async def lifespan(app):
    setup()
    topics.migrate()
    app.state.organizer = topics.Organizer()
    group_task = asyncio.create_task(app.state.organizer.run())
    app.state.card_worker = tracking.CardWorker()
    task = asyncio.create_task(app.state.card_worker.run())
    try:
        yield
    finally:
        for background in (group_task, task):
            background.cancel()
        await asyncio.gather(group_task, task, return_exceptions=True)


app = FastAPI(title="续页", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["localhost", "127.0.0.1", "testserver"]
    + [
        host.strip()
        for host in os.getenv("XUDU_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    ],
)


@app.middleware("http")
async def same_origin(request, next_call):
    origin = request.headers.get("origin")
    if (
        request.method not in ("GET", "HEAD")
        and origin
        and urlsplit(origin).netloc != request.headers.get("host")
    ):
        return JSONResponse({"detail": "请从阅读页操作。"}, status_code=403)
    response = await next_call(request)
    if request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
    return response


def owner(request: Request):
    with db() as c:
        reader = accounts.identity(c, request)
    if not reader:
        raise HTTPException(401, "请刷新以恢复阅读工作区。")
    return reader


def book(c, who, bid):
    accounts.session_state(c, who)
    row = c.execute(
        "SELECT s.*,d.pages,d.chapters,d.last FROM shelves s JOIN documents d ON s.doc=d.digest WHERE s.owner=? AND s.id=?",
        (who.owner, bid),
    ).fetchone()
    if not row:
        raise HTTPException(404, "没有找到这本书。")
    return row


def state(c, b):
    chapters = decode(b, "chapters")
    location = c.execute(
        "SELECT pos,text,page FROM sentences WHERE doc=? AND pos=?",
        (b["doc"], b["progress"]),
    ).fetchone()
    chapters = [
        {**ch, "title": ch["title"] if ch["start"] <= b["progress"] else ch["label"]}
        for ch in chapters
    ]
    threads = []
    for r in c.execute(
        "SELECT * FROM threads WHERE book=? ORDER BY touched DESC,rowid DESC",
        (b["id"],),
    ):
        first = c.execute(
            "SELECT question,pos FROM turns WHERE thread=? ORDER BY created LIMIT 1",
            (r["id"],),
        ).fetchone()
        threads.append(
            {
                "id": r["id"],
                "title": first["question"][:30]
                if first and first["pos"] <= b["progress"]
                else "阅读对话",
            }
        )
    return dict(
        sample=b["doc"] in (SAMPLE_DIGEST, SHOWCASE_DIGEST),
        sample_preview_page={SAMPLE_DIGEST: 11, SHOWCASE_DIGEST: 168}.get(b["doc"]),
        sample_author={SAMPLE_DIGEST: "吴承恩 著 · 百回本", SHOWCASE_DIGEST: "简·奥斯汀 著 · 英文版"}.get(b["doc"]),
        id=b["id"],
        title=b["title"],
        pages=b["pages"],
        rev=b["rev"],
        last=b["last"],
        progress=b["progress"],
        previous=b["previous"],
        location=dict(location) if location else None,
        chapters=chapters,
        threads=threads,
        **topic_state(c, b),
    )


def topic_state(c, b):
    rows = topics.visible(c, b["id"], b["progress"])
    return dict(
        tracked=[r["id"] for r in rows],
        card_topics=topics.summaries(rows),
        topics_pending=any(r["id"] == r["root"] and not r["reviewed"] for r in rows),
    )


@app.get("/api/books/{bid}/topics")
async def book_topics(bid: str, rev: int, who=Depends(owner)):
    with db() as c:
        result = topic_state(c, current(c, who, bid, rev))
    if result["topics_pending"]:
        app.state.organizer.wake.set()
    return result


def change_active(c, who, bid):
    active = accounts.session_state(c, who)["active"]
    if active == bid:
        return
    if who.username:
        c.execute(
            "UPDATE account_sessions SET active=?,epoch=epoch+1 WHERE token=?",
            (bid, who.session),
        )
    else:
        if active:
            c.execute(
                "UPDATE shelves SET rev=rev+1 WHERE id=? AND owner=?",
                (active, who.owner),
            )
        c.execute("UPDATE visitors SET active=? WHERE id=?", (bid, who.owner))


def attach(c, who, digest, title):
    accounts.session_state(c, who, same_epoch=True)
    c.execute(
        "INSERT OR IGNORE INTO shelves(id,owner,doc,title) VALUES(?,?,?,?)",
        (uid(), who.owner, digest, title[:120]),
    )
    bid = c.execute(
        "SELECT id FROM shelves WHERE owner=? AND doc=?", (who.owner, digest)
    ).fetchone()[0]
    if not c.execute("SELECT 1 FROM threads WHERE book=?", (bid,)).fetchone():
        c.execute("INSERT INTO threads VALUES(?,?,?)", (uid(), bid, time.time_ns()))
    change_active(c, who, bid)
    return bid


@app.get("/api/library")
def library(response: Response, request: Request):
    with db() as c:
        who = accounts.identity(c, request)
    if not who:
        file, title = (SHOWCASE, "傲慢与偏见") if usage.demo() else (SAMPLE, "西游记")
        digest = import_pdf(file.read_bytes())
        visitor = uid()
        who = accounts.ReaderIdentity(visitor, visitor)
        with db() as c:
            c.execute("INSERT INTO visitors VALUES(?,NULL)", (visitor,))
            attach(c, who, digest, title)
        accounts.cookie(response, accounts.GUEST_COOKIE, visitor, 365 * 86400)
    with db() as c:
        active = accounts.session_state(c, who)["active"]
        books = [
            dict(r)
            for r in c.execute(
                "SELECT id,title FROM shelves WHERE owner=? ORDER BY rowid DESC",
                (who.owner,),
            )
        ]
        return dict(
            books=books,
            book=state(c, book(c, who, active)) if active else None,
            username=who.username,
            invitation_required=bool(os.getenv("XUDU_INVITATION")),
            demo=usage.demo(),
        )


@app.get("/healthz")
def health():
    with db() as c:
        c.execute("SELECT 1 FROM documents LIMIT 1").fetchone()
    return {"ok": True}


@app.get("/api/session")
def session_info(who=Depends(owner)):
    with db() as c:
        active = accounts.session_state(c, who)["active"]
        rev = book(c, who, active)["rev"] if active else None
    return dict(username=who.username, book=active, rev=rev)


@app.post("/api/account/register")
def register(data: accounts.Credentials, request: Request, response: Response):
    usage.private_feature()
    with db() as c:
        accounts.throttle(c, request, "register")
    username = accounts.validate_registration(data)
    salt = secrets.token_bytes(16)
    hashed = accounts.password_digest(data.password, salt)
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        guest = accounts.identity(c, request)
        if guest is None and request.cookies.get(accounts.GUEST_COOKIE):
            raise HTTPException(409, "访客书架已变更，请刷新后再注册。")
        if guest and guest.username:
            raise HTTPException(409, "请先退出当前账号。")
        account = uid()
        try:
            c.execute(
                "INSERT INTO accounts VALUES(?,?,?,?,?)",
                (account, username, accounts.normalized_name(username), salt, hashed),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "这个用户名已被使用。") from exc
        active = None
        if guest:
            active = accounts.session_state(c, guest)["active"]
            c.execute(
                "UPDATE shelves SET owner=? WHERE owner=?", (account, guest.owner)
            )
            c.execute("DELETE FROM visitors WHERE id=?", (guest.owner,))
            response.delete_cookie(accounts.GUEST_COOKIE)
        accounts.start_session(c, account, active, response)
    return {"username": username}


@app.post("/api/account/login")
def login(data: accounts.Credentials, request: Request, response: Response):
    usage.private_feature()
    with db() as c:
        accounts.throttle(c, request, "login")
        account = c.execute(
            "SELECT * FROM accounts WHERE name_key=?",
            (accounts.normalized_name(data.username),),
        ).fetchone()
    hashed = accounts.password_digest(
        data.password, account["salt"] if account else bytes(16)
    )
    if not account or not secrets.compare_digest(account["password"], hashed):
        raise HTTPException(401, "用户名或密码不正确。")
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        who = accounts.identity(c, request)
        if who and who.username:
            c.execute("DELETE FROM account_sessions WHERE token=?", (who.session,))
        recent = c.execute(
            """SELECT s.id FROM shelves s LEFT JOIN threads h ON h.book=s.id
                              WHERE s.owner=? GROUP BY s.id ORDER BY max(h.touched) DESC,s.rowid DESC LIMIT 1""",
            (account["id"],),
        ).fetchone()
        accounts.start_session(
            c, account["id"], recent[0] if recent else None, response
        )
    return {"username": account["username"]}


@app.post("/api/account/logout")
def logout(response: Response, who=Depends(owner)):
    if who.username:
        with db() as c:
            c.execute("DELETE FROM account_sessions WHERE token=?", (who.session,))
    response.delete_cookie(accounts.ACCOUNT_COOKIE)
    return {"done": True}


@app.get("/sample.pdf")
def sample():
    file = SHOWCASE if usage.demo() else SAMPLE
    return FileResponse(file, filename=file.name)


@app.post("/api/books")
def upload(file: UploadFile = File(), who=Depends(owner)):
    usage.private_feature()
    raw = file.file.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise HTTPException(413, "文件超过 32 MB。")
    if not raw.startswith(b"%PDF-"):
        raise HTTPException(422, "请选择文字版 PDF。")
    digest = import_pdf(raw)
    with db() as c:
        bid = attach(c, who, digest, Path(file.filename or "我的书").stem)
        added = state(c, book(c, who, bid))
        readable = {
            row["page"] for row in c.execute(
                "SELECT DISTINCT page FROM sentences WHERE doc=?", (digest,)
            )
        }
        added["import_report"] = {
            "readable_pages": len(readable),
            "missing_pages": [
                page for page in range(1, added["pages"] + 1) if page not in readable
            ],
        }
        return added


@app.post("/api/books/{bid}/open")
def open_book(bid: str, who=Depends(owner)):
    with db() as c:
        book(c, who, bid)
        change_active(c, who, bid)
        return state(c, book(c, who, bid))


class Position(BaseModel):
    pos: int = Field(ge=0)
    rev: int


def current(c, who, bid, rev):
    b = book(c, who, bid)
    active = accounts.session_state(c, who, same_epoch=True)["active"]
    if active != bid or rev != b["rev"]:
        raise HTTPException(409, "阅读位置或书籍已变动。请按当前位置重试。")
    return b


@app.put("/api/books/{bid}/progress")
async def progress(bid: str, data: Position, who=Depends(owner)):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        b = current(c, who, bid, data.rev)
        if data.pos > b["last"]:
            raise HTTPException(422, "位置超出这本书。")
        if data.pos != b["progress"]:
            c.execute(
                "UPDATE shelves SET previous=progress,progress=?,rev=rev+1 WHERE id=?",
                (data.pos, bid),
            )
        result = state(c, book(c, who, bid))
    app.state.card_worker.wake.set()
    app.state.organizer.wake.set()
    return result


class Revision(BaseModel):
    rev: int


@app.post("/api/books/{bid}/turns/{turn}/track")
async def track(bid: str, turn: str, data: Revision, who=Depends(owner)):
    def source(c):
        b = current(c, who, bid, data.rev)
        row = c.execute(
            """SELECT t.*,json_extract(t.answer,'$.scope_end') AS scope_end FROM turns t JOIN threads h ON t.thread=h.id
            WHERE h.book=? AND t.id=? AND t.pos<=?""",
            (bid, turn, b["progress"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "当前位置没有这条回答。")
        return b, row

    usage.reader.set(who.owner)
    async with app.state.organizer.lock:
        with db() as c:
            b, row = source(c)
            candidates = topics.visible(c, bid, b["progress"])
            known = next((v["root"] for v in candidates if v["id"] == turn), None)
            if known:
                return {"id": known, **topic_state(c, b)}
        deferred = False
        try:
            target = await topics.match(row, candidates)
        except HTTPException as exc:
            if exc.status_code not in (429, 502, 503):
                raise
            target, deferred = None, True
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            b, row = source(c)
            tracking.start(c, b, row)
            root = turn
            if (
                target
                and c.execute(
                    "SELECT 1 FROM card_links WHERE turn=? AND root=?", (target, target)
                ).fetchone()
            ):
                root = topics.join(c, target, turn)
            c.execute(
                """UPDATE card_links SET reviewed=0 WHERE root=turn AND turn IN
                (SELECT q.turn FROM tracked_questions q JOIN turns t ON q.turn=t.id WHERE q.book=? AND t.pos>=?)""",
                (bid, row["pos"] if deferred else b["progress"] + 1),
            )
            summary = topic_state(c, b)
    app.state.card_worker.wake.set()
    if deferred:
        app.state.organizer.wake.set()
    return {"id": root, **summary}


@app.get("/api/books/{bid}/cards")
def list_cards(bid: str, rev: int, who=Depends(owner)):
    with db() as c:
        c.execute("BEGIN")
        return tracking.cards(c, current(c, who, bid, rev))


@app.post("/api/books/{bid}/cards/{turn}/retry")
async def retry_card(bid: str, turn: str, data: Revision, who=Depends(owner)):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        b = current(c, who, bid, data.rev)
        tracked = c.execute(
            """SELECT json_extract(t.answer,'$.scope_end') FROM tracked_questions q JOIN turns t ON t.id=q.turn
                            WHERE q.book=? AND q.turn=? AND t.pos<=?""",
            (bid, turn, b["progress"]),
        ).fetchone()
        if not tracked:
            raise HTTPException(404)
        cutoff = tracked[0]
        c.execute(
            "DELETE FROM card_failures WHERE turn=? AND target=?",
            (turn, min(cutoff, b["progress"]) if cutoff is not None else b["progress"]),
        )
    app.state.card_worker.wake.set()
    return {"id": turn}


@app.get("/api/books/{bid}/search")
def search_read(
    bid: str, q: str = Query(min_length=1, max_length=100), who=Depends(owner)
):
    with db() as c:
        b = book(c, who, bid)
        rows = c.execute(
            "SELECT pos,page,text FROM sentences WHERE doc=? AND pos<=? AND instr(text,?)>0 ORDER BY pos DESC LIMIT 25",
            (b["doc"], b["progress"], q.strip()),
        )
        return [dict(r) for r in rows] if q.strip() else []


@app.get("/api/books/{bid}/pages/{page}")
def page_data(bid: str, page: int, read_only: bool = False, who=Depends(owner)):
    with db() as c:
        b = book(c, who, bid)
        if not 1 <= page <= b["pages"]:
            raise HTTPException(404, "没有这一页。")
        rows = [
            dict(pos=r["pos"], text=r["text"], boxes=decode(r, "boxes"))
            for r in c.execute(
                "SELECT pos,text,boxes FROM sentences WHERE doc=? AND page=? AND pos<=? ORDER BY pos",
                (b["doc"], page, b["progress"] if read_only else b["last"]),
            )
        ]
        return {"rows": rows}


@app.get("/api/books/{bid}/pages/{page}/image")
def preview_image(bid: str, page: int, who=Depends(owner)):
    with db() as c:
        b = book(c, who, bid)
    if not 1 <= page <= b["pages"]:
        raise HTTPException(404)
    with pymupdf.open(DATA / (b["doc"] + ".pdf")) as pdf:
        png = (
            pdf[page - 1]
            .get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
            .tobytes("png")
        )
    return Response(png, media_type="image/png")


def thread_check(c, bid, tid):
    if not c.execute(
        "SELECT 1 FROM threads WHERE book=? AND id=?", (bid, tid)
    ).fetchone():
        raise HTTPException(404)


def erase_cards(c, members):
    for turn in members:
        c.execute("DELETE FROM card_checks WHERE turn=?", (turn,))
        c.execute("DELETE FROM card_failures WHERE turn=?", (turn,))
        c.execute("DELETE FROM card_links WHERE turn=?", (turn,))
        c.execute("DELETE FROM tracked_questions WHERE turn=?", (turn,))
        c.execute("DELETE FROM turns WHERE id=? AND thread IS NULL", (turn,))


@app.delete("/api/books/{bid}/cards/{cid}")
def remove_card(bid: str, cid: str, rev: int, who=Depends(owner)):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        b = current(c, who, bid, rev)
        visible = topics.visible(c, bid, b["progress"])
        if not any(v["root"] == cid for v in visible):
            raise HTTPException(404, "没有找到这张卡。")
        members = c.execute(
            "SELECT turn FROM card_links WHERE root=?", (cid,)
        ).fetchall()
        erase_cards(c, [r[0] for r in members])
        return state(c, b)


@app.delete("/api/books/{bid}/threads/{tid}")
def remove_thread(bid: str, tid: str, rev: int, who=Depends(owner)):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        b = current(c, who, bid, rev)
        thread_check(c, bid, tid)
        # Saved cards live on the shelf independently of their source conversation.
        for row in c.execute(
            """SELECT t.* FROM turns t JOIN tracked_questions q ON t.id=q.turn
                WHERE t.thread=?""",
            (tid,),
        ).fetchall():
            context = tracking.previous_turns(c, row["id"], row["pos"])
            c.execute(
                "UPDATE card_links SET context=? WHERE turn=?",
                (json.dumps(context, ensure_ascii=False), row["id"]),
            )
        c.execute(
            """UPDATE turns SET thread=NULL,spoiler=NULL,state='done'
                     WHERE thread=? AND id IN (SELECT turn FROM tracked_questions)""",
            (tid,),
        )
        c.execute("DELETE FROM turns WHERE thread=?", (tid,))
        c.execute("DELETE FROM threads WHERE id=?", (tid,))
        return state(c, b)


@app.delete("/api/books/{bid}")
def remove_book(bid: str, who=Depends(owner)):
    usage.private_feature()
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        book(c, who, bid)
        erase_cards(
            c,
            [
                r[0]
                for r in c.execute(
                    "SELECT turn FROM tracked_questions WHERE book=?", (bid,)
                )
            ],
        )
        c.execute(
            "DELETE FROM turns WHERE thread IN (SELECT id FROM threads WHERE book=?)",
            (bid,),
        )
        c.execute("DELETE FROM threads WHERE book=?", (bid,))
        c.execute("DELETE FROM shelves WHERE id=?", (bid,))
        replacement = c.execute(
            "SELECT id FROM shelves WHERE owner=? ORDER BY rowid DESC LIMIT 1",
            (who.owner,),
        ).fetchone()
        active = replacement[0] if replacement else None
        if who.username:
            c.execute(
                "UPDATE account_sessions SET active=?,epoch=epoch+1 WHERE account=? AND active=?",
                (active, who.owner, bid),
            )
        else:
            c.execute(
                "UPDATE visitors SET active=? WHERE id=? AND active=?",
                (active, who.owner, bid),
            )

    return {"deleted": bid}


@app.post("/api/books/{bid}/threads")
def new_thread(bid: str, who=Depends(owner)):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        book(c, who, bid)
        empty = c.execute(
            """SELECT id FROM threads WHERE book=? AND NOT EXISTS
            (SELECT 1 FROM turns WHERE thread=threads.id) ORDER BY touched DESC LIMIT 1""",
            (bid,),
        ).fetchone()
        if empty:
            c.execute(
                "UPDATE threads SET touched=? WHERE id=?", (time.time_ns(), empty["id"])
            )
        else:
            c.execute("INSERT INTO threads VALUES(?,?,?)", (uid(), bid, time.time_ns()))
        return state(c, book(c, who, bid))


TURN_WITH_BOOKMARK = """SELECT t.*, s.text AS bookmark FROM turns t
    LEFT JOIN sentences s ON s.doc=? AND s.pos=t.pos"""


def consent_scope(who):
    return [who.session, who.epoch] if who.username else None


def present(r, b, who):
    if r["pos"] > b["progress"]:
        return dict(id=r["id"], hidden=True)
    original = decode(r, "answer")
    answer = agent.evidence_only_if_uncertain(original.get("reader_version", original))
    answer = {
        key: value for key, value in answer.items()
        if key not in {"_consent", "_continuation_source", "_reference_turn"}
    }
    if answer["status"] in ("limited", "unknown"):
        # The original turn's bookmark stays authoritative when the reader advances or rewinds.
        cutoff = original.get("scope_end")
        shortened = cutoff is not None and cutoff < r["pos"]
        answer["reader_note"] = agent.reader_note(
            r["question"],
            answer,
            cutoff if shortened else r["pos"],
            None if shortened else r["bookmark"],
        )
    return dict(
        id=r["id"],
        question=r["question"],
        answer=answer,
        spoiler=bool(r["spoiler"]),
        pending=(
            r["state"] == "pending"
            and r["rev"] == b["rev"]
            and original.get("_consent") == consent_scope(who)
        ),
        pos=r["pos"],
    )


@app.post("/api/books/{bid}/turns/{turn}/rephrase")
async def rephrase(bid: str, turn: str, data: Revision, who=Depends(owner)):
    def original(c):
        b = current(c, who, bid, data.rev)
        row = c.execute(
            TURN_WITH_BOOKMARK
            + " JOIN threads h ON t.thread=h.id WHERE t.id=? AND h.book=?",
            (b["doc"], turn, bid),
        ).fetchone()
        if not row or row["pos"] > b["progress"]:
            raise HTTPException(404)
        answer = decode(row, "answer")
        if answer["status"] != "answer" or not answer["claims"]:
            raise HTTPException(422, "这条回答没有可以改写的已读结论。")
        return b, row, answer

    with db() as c:
        b, row, answer = original(c)
        if answer.get("conversational") or "reader_version" in answer:
            return present(row, b, who)
    # A new bookmark must never extend the evidence available to an old question.
    claims = await agent.phrase_for_reader(row["question"], answer["claims"])
    approved, _, _ = await agent.verify(row["question"], claims, "", "answer")
    if not approved:
        raise HTTPException(422, "换个说法后依据不足，已保留原回答。")
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        b, row, answer = original(c)
        if "reader_version" not in answer:
            answer["reader_version"] = dict(
                status="answer",
                claims=approved,
                background=answer.get("background", ""),
                conversational=True,
            )
            c.execute(
                "UPDATE turns SET answer=? WHERE id=?",
                (json.dumps(answer, ensure_ascii=False), turn),
            )
        return present(
            c.execute(
                TURN_WITH_BOOKMARK + " WHERE t.id=?", (b["doc"], turn)
            ).fetchone(),
            b,
            who,
        )


@app.get("/api/books/{bid}/threads/{tid}")
def timeline(bid: str, tid: str, who=Depends(owner)):
    with db() as c:
        b = book(c, who, bid)
        thread_check(c, bid, tid)
        c.execute("UPDATE threads SET touched=? WHERE id=?", (time.time_ns(), tid))
        return [
            present(r, b, who)
            for r in c.execute(
                TURN_WITH_BOOKMARK + " WHERE t.thread=? ORDER BY t.created",
                (b["doc"], tid),
            )
        ]


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=1800)
    rev: int
    selected: int = 0
    request_id: UUID | None = None
    continue_from: str | None = Field(default=None, min_length=1, max_length=64)


@app.post("/api/books/{bid}/threads/{tid}/ask")
async def ask(bid: str, tid: str, data: Question, request: Request, who=Depends(owner)):
    usage.reader.set(who.owner)
    return await streaming.deliver(
        request,
        who.session,
        data.request_id,
        lambda report: answer_question(bid, tid, data, who, report),
    )


def history_entry(row):
    saved = decode(row, "answer")
    answer = agent.evidence_only_if_uncertain(saved.get("reader_version", saved))
    return {
        "q": row["question"],
        "a": [cl["text"] for cl in answer["claims"]],
        "pos": row["pos"],
        "scope_end": saved.get("scope_end"),
        "anchor_question": saved.get("anchor_question"),
        "answer": answer,
    }


def history_rows(c, tid, progress, *, count=6, before=None):
    return c.execute(
        """SELECT id,question,answer,pos,rev,selected,state,created
        FROM turns WHERE thread=? AND pos<=? AND (? IS NULL OR created<?)
        ORDER BY created DESC LIMIT ?""",
        (tid, progress, before, before, count),
    ).fetchall()


def find_tally_source(c, tid, limit, requested):
    row = c.execute(
        """SELECT question,answer,pos FROM turns
        WHERE thread=? AND pos<=?
          AND (? IS NULL OR id=?)
          AND (
            json_type(answer,'$.tally')='object'
            OR json_type(answer,'$.reader_version.tally')='object'
            OR instr(coalesce(json_extract(answer,'$.reader_note'),''),'确切总数还没有核全')>0
          )
        ORDER BY created DESC LIMIT 1""",
        (tid, limit, requested, requested),
    ).fetchone()
    if not row and requested:
        raise HTTPException(404, "当前阅读位置看不到这条统计。")
    return history_entry(row) if row else None


async def answer_question(bid, tid, data, who, report):
    q = data.question.strip()
    if not q:
        raise HTTPException(422, "请输入你的问题。")
    continuing = not data.selected and agent.conversation_action(q) == "continue"
    if data.continue_from and not continuing:
        raise HTTPException(422, "请从统计结果继续核对。")
    with db() as c:
        b = current(c, who, bid, data.rev)
        thread_check(c, bid, tid)
        if not 0 <= data.selected <= b["progress"]:
            raise HTTPException(409, "选段超出已读位置。")
        if data.request_id:
            prior = c.execute(
                TURN_WITH_BOOKMARK + " WHERE t.id=?", (b["doc"], str(data.request_id))
            ).fetchone()
            if prior:
                if (
                    prior["thread"] != tid
                    or prior["rev"] != data.rev
                    or prior["question"] != q
                    or prior["selected"] != data.selected
                    or decode(prior, "answer").get("_continuation_source") != data.continue_from
                ):
                    raise HTTPException(409, "问题已变动，请重新发送。")
                return present(prior, b, who)
        history = []
        repeated = None
        for row in history_rows(c, tid, b["progress"]):
            # Asking the same declined question again creates a fresh consent, with the same read-only evidence.
            ordinary = agent.evidence_only_if_uncertain(decode(row, "answer"))
            if (
                not history
                and row["state"] == "done"
                and row["rev"] == data.rev
                and row["selected"] == data.selected
                and row["question"] == q
                and ordinary["status"] == "limited"
                and not continuing
            ):
                repeated = ordinary
            history.append(history_entry(row))
        tally_source = find_tally_source(c, tid, b["progress"], data.continue_from) if continuing else None
    anchor = None
    references_earlier_topic = not data.selected and not continuing and agent.EARLIER_TOPIC.search(q)
    if references_earlier_topic:
        limit = b["progress"]
        for match in agent.CHAPTER_END.finditer(q):
            end = agent.chapter_end(b, agent.ordinal(match[1] or match[2]))
            if end is not None:
                limit = min(limit, end)
        with db() as c:
            rows = history_rows(c, tid, limit, count=48)
        entries = [
            {**history_entry(row), "id": row["id"], "created": row["created"]}
            for row in rows
        ]
        entries = [
            h for h in entries
            if h["answer"]["status"] in ("answer", "unknown", "limited")
        ]
        report("正在找你前面聊过的话题…")
        anchor = await agent.choose_history_anchor(q, entries)
        if anchor:
            history = [anchor] + [h for h in entries if h["created"] < anchor["created"]][:5]
            repeated = None
    if references_earlier_topic and not anchor:
        answer = dict(
            status="clarify", claims=[], background="",
            reader_note="我还没确定你指的是前面哪条问题。可以补个人名、事件或那条问题里的几个字。",
        )
    else:
        answer = repeated or await agent.respond(
            b, q, b["progress"], data.selected, history[::-1], report=report,
            **({"tally_source": tally_source} if tally_source else {}),
        )
    if anchor:
        answer["_reference_turn"] = anchor["id"]
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        b = current(c, who, bid, data.rev)
        thread_check(c, bid, tid)
        turn = str(data.request_id) if data.request_id else uid()
        status = "pending" if answer["status"] == "limited" else "done"
        if status == "pending" and who.username:
            answer = {**answer, "_consent": consent_scope(who)}
        if data.continue_from:
            answer = {**answer, "_continuation_source": data.continue_from}
        now = time.time_ns()
        c.execute(
            "INSERT INTO turns VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                turn,
                tid,
                q,
                b["progress"],
                data.rev,
                data.selected,
                status,
                json.dumps(answer, ensure_ascii=False),
                None,
                now,
            ),
        )
        c.execute("UPDATE threads SET touched=? WHERE id=?", (now, tid))
        return present(
            c.execute(
                TURN_WITH_BOOKMARK + " WHERE t.id=?", (b["doc"], turn)
            ).fetchone(),
            b,
            who,
        )


@app.post("/api/requests/{rid}/stop")
async def stop_request(rid: UUID, who=Depends(owner)):
    task = streaming.running.get((who.session, rid))
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return {"stopped": True}
    return {"stopped": False}


class Choice(BaseModel):
    request_id: UUID | None = None
    action: str
    rev: int


@app.post("/api/books/{bid}/turns/{turn}/choice")
async def choice(
    bid: str, turn: str, data: Choice, request: Request, who=Depends(owner)
):
    usage.reader.set(who.owner)
    return await streaming.deliver(
        request,
        who.session,
        data.request_id,
        lambda report: answer_choice(bid, turn, data, who, report),
    )


async def answer_choice(bid, turn, data, who, report):
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        b = current(c, who, bid, data.rev)
        r = c.execute(
            "SELECT t.* FROM turns t JOIN threads th ON th.id=t.thread WHERE th.book=? AND t.id=?",
            (bid, turn),
        ).fetchone()
        if not r or r["pos"] > b["progress"]:
            raise HTTPException(404)
        saved = decode(r, "answer")
        consent_matches = saved.get("_consent") == consent_scope(who)
        if r["spoiler"] and (
            data.action == "reveal"
            or (data.action == "allow" and r["rev"] == b["rev"] and consent_matches)
        ):
            return decode(r, "spoiler")
        if (
            data.action not in ("allow", "decline")
            or not consent_matches
            or r["state"] != "pending"
            or r["rev"] != b["rev"]
        ):
            raise HTTPException(409, "这次确认已结束，请重新提问。")
        c.execute(
            "UPDATE turns SET state=? WHERE id=?",
            ("checking" if data.action == "allow" else "done", turn),
        )
        if data.action == "decline":
            return {"done": True}
        cutoff = saved.get("scope_end")
        earlier = c.execute(
            "SELECT created FROM turns WHERE id=? AND thread=? AND pos<=? AND created<?",
            (saved.get("_reference_turn"), r["thread"], r["pos"], r["created"]),
        ).fetchone()
        before = earlier["created"] + 1 if earlier else r["created"]
        recent = history_rows(c, r["thread"], r["pos"], before=before)
        history = [history_entry(v) for v in reversed(recent)]
        if cutoff is not None:
            b = {**dict(b), "last": min(cutoff, b["last"])}
            history = [h for h in history if h["pos"] <= b["last"]]
    try:
        result = await agent.respond(
            b,
            r["question"],
            min(r["pos"], b["last"]),
            r["selected"] if r["selected"] <= b["last"] else 0,
            history,
            allow=True,
            report=report,
        )
        with db() as c:
            c.execute("BEGIN IMMEDIATE")
            current(c, who, bid, r["rev"])
            thread_check(c, bid, r["thread"])
            c.execute(
                "UPDATE turns SET spoiler=?,state='done' WHERE id=?",
                (json.dumps(result, ensure_ascii=False), turn),
            )
        return result
    except (Exception, asyncio.CancelledError):
        with db() as c:
            c.execute(
                "UPDATE turns SET state='pending' WHERE id=? AND state='checking'",
                (turn,),
            )
        raise


@app.get("/api/books/{bid}/turns/{turn}/citation/{position}")
def citation_image(
    bid: str,
    turn: str,
    position: int,
    rev: int,
    spoiler: bool = False,
    who=Depends(owner),
):
    with db() as c:
        b = current(c, who, bid, rev)
        row = c.execute(
            "SELECT t.* FROM turns t JOIN threads th ON th.id=t.thread WHERE th.book=? AND t.id=?",
            (bid, turn),
        ).fetchone()
        if (
            not row
            or row["pos"] > b["progress"]
            or (not spoiler and position > b["progress"])
        ):
            raise HTTPException(404)
        original = decode(row, "answer")
        cutoff = original.get("scope_end")
        if cutoff is not None and position > cutoff:
            raise HTTPException(404)
        answer = decode(row, "spoiler") if spoiler else original
        ref_texts = [
            ref["text"]
            for claim in (answer or {}).get("claims", [])
            for ref in claim["refs"]
            if ref["pos"] == position
        ]
        if not ref_texts:
            raise HTTPException(404)
        source = c.execute(
            "SELECT page,text,boxes FROM sentences WHERE doc=? AND pos=?",
            (b["doc"], position),
        ).fetchone()
        if spoiler and source["text"] not in ref_texts:
            raise HTTPException(404, "本题只显示支持答案的原文摘句。")
    # Copy each line separately. A bounding rectangle spanning two lines might expose the next sentence.
    with pymupdf.open(DATA / (b["doc"] + ".pdf")) as original, pymupdf.open() as cut:
        page = original[source["page"] - 1]
        w, h = page.rect.width, page.rect.height
        boxes = [
            pymupdf.Rect(x * w, y * h, x1 * w, y1 * h) & page.rect
            for x, y, x1, y1 in decode(source, "boxes")
        ]
        boxes = [box for box in boxes if not box.is_empty]
        if not boxes:
            raise HTTPException(404)
        output = cut.new_page(
            width=max(box.width for box in boxes) + 4,
            height=sum(box.height + 2 for box in boxes) + 2,
        )
        y = 1.0
        for box in boxes:
            strip = page.get_pixmap(
                matrix=pymupdf.Matrix(1.7, 1.7), clip=box, alpha=False
            )
            output.insert_image(
                pymupdf.Rect(1, y, 1 + box.width, y + box.height), pixmap=strip
            )
            y += box.height + 2
        return Response(
            output.get_pixmap(matrix=pymupdf.Matrix(1.7, 1.7)).tobytes("png"),
            media_type="image/png",
        )


dist = ROOT / "frontend" / "dist"
if dist.exists():
    app.mount("/", StaticFiles(directory=dist, html=True), name="web")
