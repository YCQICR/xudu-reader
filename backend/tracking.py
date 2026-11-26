"""Persist incremental reading checks. A checkpoint only becomes visible at its own bookmark."""

import asyncio
import hashlib
import json
import logging
import time

from . import agent, topics, usage
from .storage import db, decode, texts

TARGET = "min(s.progress, coalesce(json_extract(t.answer, '$.scope_end'), s.progress))"


def fact_id(claim):
    source = json.dumps([claim["text"], sorted(ref["pos"] for ref in claim["refs"])])
    return hashlib.sha256(source.encode()).hexdigest()[:16]


# One worker per local application. The database records completed work, including empty checks.
class CardWorker:
    def __init__(self):
        self.wake = asyncio.Event()

    async def run(self):
        while True:
            self.wake.clear()
            with db() as c:
                job = c.execute(f"""
                    SELECT q.turn,q.created,coalesce(json_extract(t.answer, '$.anchor_question'),t.question) AS question,
                    t.selected,t.pos AS origin,s.doc,s.owner,{TARGET} AS target
                    FROM tracked_questions q JOIN turns t ON t.id=q.turn JOIN shelves s ON s.id=q.book
                    JOIN card_links l ON l.turn=q.turn AND l.root=q.turn
                    WHERE {TARGET}>t.pos AND NOT EXISTS
                      (SELECT 1 FROM card_checks k WHERE k.turn=q.turn AND k.pos>={TARGET})
                    AND NOT EXISTS (SELECT 1 FROM card_failures f WHERE f.turn=q.turn AND f.target={TARGET})
                    ORDER BY COALESCE((SELECT max(pos) FROM card_checks k WHERE k.turn=q.turn
                        AND k.pos<=s.progress),0), q.created LIMIT 1
                """).fetchone()
            if not job:
                await self.wake.wait()
                continue
            try:
                usage.reader.set(job["owner"])
                await advance(job)
            except Exception:
                logging.getLogger(__name__).exception("Reading card update failed")
                with db() as c:
                    c.execute(
                        "INSERT OR IGNORE INTO card_failures SELECT turn,? FROM tracked_questions WHERE turn=? AND created=?",
                        (job["target"], job["turn"], job["created"]),
                    )


def start(c, b, turn):
    answer = agent.evidence_only_if_uncertain(decode(turn, "answer"))
    if answer["status"] in ("off_topic", "clarify"):
        from fastapi import HTTPException

        raise HTTPException(422, "请先选一个明确的书内问题。")
    c.execute(
        "INSERT OR IGNORE INTO tracked_questions VALUES(?,?,?)",
        (turn["id"], b["id"], time.time_ns()),
    )
    c.execute(
        "INSERT OR IGNORE INTO card_links(turn,root,reviewed) VALUES(?,?,1)",
        (turn["id"], turn["id"]),
    )
    # Only the ordinary answer can seed a card. The one-off spoiler lives in a separate column.
    c.execute(
        "INSERT OR IGNORE INTO card_checks VALUES(?,?,NULL,?)",
        (turn["id"], turn["pos"], json.dumps(answer["claims"], ensure_ascii=False)),
    )


def chain(c, turn, target):
    return c.execute(
        """
        WITH RECURSIVE visible(pos,parent,additions) AS (
            SELECT pos,parent,additions FROM card_checks WHERE turn=? AND pos=(
                SELECT max(pos) FROM card_checks WHERE turn=? AND pos<=?)
            UNION ALL
            SELECT k.pos,k.parent,k.additions FROM card_checks k JOIN visible v ON k.pos=v.parent WHERE k.turn=?
        ) SELECT pos,additions FROM visible ORDER BY pos
    """,
        (turn, turn, target, turn),
    ).fetchall()


def cards(c, b):
    result = []
    visible = topics.visible(c, b["id"], b["progress"])
    origins = {row["id"]: row["pos"] for row in visible}
    for topic in topics.summaries(visible):
        versions = []
        by_pos = {}
        checked = 0
        target = c.execute(
            f"""SELECT {TARGET} FROM turns t JOIN tracked_questions q ON q.turn=t.id
            JOIN shelves s ON s.id=q.book WHERE t.id=?""",
            (topic["id"],),
        ).fetchone()[0]
        for member in topic["members"]:
            origin = origins[member]
            by_pos.setdefault(origin, [])
            latest = c.execute(
                "SELECT max(pos) FROM card_checks WHERE turn=?", (member,)
            ).fetchone()[0]
            if latest is None:
                continue
            if member == topic["id"]:
                checked = min(latest, target)
            for check in chain(c, member, latest):
                for claim in decode(check, "additions"):
                    reveal = max([origin] + [ref["pos"] for ref in claim["refs"]])
                    if reveal <= b["progress"]:
                        by_pos.setdefault(reveal, []).append(claim)
        seen = set()
        current = {}
        for pos, claims in sorted(by_pos.items()):
            additions = []
            for claim in claims:
                signature = fact_id(claim)
                if signature not in seen:
                    additions.append(claim)
                    seen.add(signature)
                    for superseded in claim.get("replaces", []):
                        current.pop(superseded, None)
                    current[signature] = claim
            if additions or not versions:
                page = c.execute(
                    "SELECT page FROM sentences WHERE doc=? AND pos=?", (b["doc"], pos)
                ).fetchone()
                versions.append(
                    dict(pos=pos, page=page[0] if page else None, claims=additions)
                )
        failed = c.execute(
            "SELECT 1 FROM card_failures WHERE turn=? AND target=?",
            (topic["id"], target),
        ).fetchone()
        result.append(
            dict(
                id=topic["id"],
                question=topic["title"],
                checked=checked,
                state="ready"
                if checked == target
                else "failed"
                if failed
                else "updating",
                versions=versions,
                current_claims=list(current.values()),
            )
        )
    return result


def previous_turns(c, turn, origin):
    earlier = c.execute(
        """SELECT question,answer FROM turns WHERE thread=(SELECT thread FROM turns WHERE id=?)
        AND created<(SELECT created FROM turns WHERE id=?) AND pos<=? ORDER BY created DESC LIMIT 3""",
        (turn, turn, origin),
    ).fetchall()
    return [
        {
            "question": r["question"],
            "claims": [
                x["text"]
                for x in agent.evidence_only_if_uncertain(decode(r, "answer"))["claims"]
            ],
        }
        for r in reversed(earlier)
    ]


async def advance(job):
    turn, target = job["turn"], job["target"]
    with db() as c:
        checks = chain(c, turn, target)
        if not checks:
            return
        base = checks[-1]["pos"]
        rows = texts(c, job["doc"], base + 1, min(base + 64, target))
        prior = {}
        for check in checks:
            for claim in decode(check, "additions"):
                for superseded in claim.get("replaces", []):
                    prior.pop(superseded, None)
                prior[fact_id(claim)] = claim
        prior = dict(list(prior.items())[-60:])
        old_facts = [
            dict(id=key, text=cl["text"], refs=cl["refs"]) for key, cl in prior.items()
        ]
        anchor = texts(c, job["doc"], max(1, base - 3), base)
        if job["selected"]:
            anchor += texts(c, job["doc"], job["selected"], job["selected"])
        # Preserve the referent of follow-up questions without exposing any later turn or spoiler reply.
        conversation = previous_turns(c, turn, job["origin"])
        saved = c.execute(
            "SELECT context FROM card_links WHERE turn=?", (turn,)
        ).fetchone()
        if saved and saved["context"]:
            conversation = json.loads(saved["context"])
    upper = rows[-1]["pos"]
    note = """读者在持续追踪一个具体问题。逐句检查新增已读原文，只提出能改变这个问题答案的事实。
同义重复、无关新情节、已经有完整答案后的旁枝不更新；没有变化返回空列表。同一批原文先提到再明确说明同一对象时，只保留最具体且有依据的结论。问题和对话中的猜测不算书证。区分角色的说法、叙述和推断。
每条必须有新增证据。如果新原文明示原有猜测已被证实、推翻，或旧结论需要修订，在 replaces 中列出受影响的既有结论 id。只有同一事项的结论发生变化才填写；需要保留说话人与确定程度。
返回 JSON，例如 {"claims":[{"text":"一条与问题直接相关的当前结论","ids":[123],"replaces":[]}]}。ids 使用材料中的句子整数编号，至少有一个来自 new。"""
    data = dict(
        question=job["question"],
        prior=old_facts,
        conversation=conversation,
        anchor=anchor,
        new=rows,
    )
    draft = agent.unpack(
        await agent.call(
            [
                {"role": "system", "content": note},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
            ]
        )
    )
    if not isinstance(draft.get("claims"), list):
        raise ValueError("Invalid card response")
    seen = {r["pos"]: r for r in anchor + rows}
    candidates = []
    prior_text = {cl["text"] for cl in prior.values()}
    for raw in draft["claims"][:9]:
        for claim in agent.grounded([raw], seen):
            if (
                any(ref["pos"] > base for ref in claim["refs"])
                and claim["text"] not in prior_text
            ):
                replacements = raw.get("replaces")
                if isinstance(replacements, list):
                    claim["replaces"] = [
                        key
                        for key in replacements
                        if isinstance(key, str) and key in prior
                    ]
                candidates.append(claim)
    additions = []
    if candidates:
        additions, _, _ = await agent.verify(
            job["question"], candidates, "", "card", prior=old_facts
        )
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        if not c.execute(
            "SELECT 1 FROM tracked_questions q JOIN card_links l ON l.turn=q.turn WHERE q.turn=? AND q.created=? AND l.root=q.turn",
            (turn, job["created"]),
        ).fetchone():
            return
        c.execute(
            "INSERT OR IGNORE INTO card_checks VALUES(?,?,?,?)",
            (turn, upper, base, json.dumps(additions, ensure_ascii=False)),
        )
        c.execute("DELETE FROM card_failures WHERE turn=? AND target=?", (turn, target))
