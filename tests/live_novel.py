"""Check one coherent plot through the actual API and DeepSeek in an isolated shelf."""

import argparse
import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path

import httpx
from fastapi.testclient import TestClient


EDITION = "92b5fd2cbc3dfad120a6dba58cdac6f87b905674837e6f2859abe5ca2247e2c5"
LETTER = "达西在信里怎么解释威克姆没拿到牧师职位的事？若要查接下来的内容，请先问我。"
EARLIER = "回到一开始问的那条：威克姆自己是怎么向伊丽莎白说他和达西的过节的？给我看那条的原句。"
REACTION = "她为什么改变了对威克姆的看法？把他最初的说法、达西来信和她的反应串起来。"
REACTION_RULES = dict(
    facts=(
        r"职位|俸禄|圣职|神职",
        r"三千|3,?000|换了.{0,5}钱|改要.{0,5}(钱|补偿)|用钱代替|换.{0,5}钱|"
        r"(?:以|用)(?:一笔)?(?:现钱|现款|现金|现银).{0,6}(?:代替|替代)",
        r"偏见|偏信|偏私|偏袒|偏心|轻信|反省|一面之词|判断|羞愧|迟疑|动摇了?[^。！？]{0,8}信任|"
        r"(?:意识到|觉得).{0,12}有一方.{0,8}(?:撒谎|说谎|欺骗)|"
        r"一方必有.{0,8}(?:撒谎|说谎|欺骗)|好感.{0,8}(?:消失|动摇)|"
        r"发现[^。！？]{0,22}(?:说法.{0,8}对不上|两种说法.{0,8}不同)",
    ),
    reasoning=(r"来信|读信|达西.{0,4}信|信中|信里|那封信",
          r"怀疑|可疑|动摇|偏|反省|重新|破绽|对不上|矛盾|冲突|看错|不再|站不住|一面之词|"
          r"(?:必有一方|一方必有).{0,8}(?:说谎|撒谎|欺骗)"),
    avoid=(
        r"信[^。！？]{0,24}(?<!没有)(?<!不能)(?<!尚未)(?<!并未)(?:证实|证明|坐实)(?:了)?威克姆[^。！？]{0,15}(?:撒谎|说谎|欺骗|说的.{0,6}假)",
        r"信[^。！？]{0,14}(?<!没有)(?<!不能)(?<!尚未)(?<!并未)推翻(?:了)?威克姆",
        r"信把威克姆[^。！？]{0,18}(?:推翻|证伪)",
    ),
)
ARC_RULES = dict(
    facts=(
        r"俸禄|牧师职位|教会职位|圣职|神职",
        r"三千|3,?000|换了.{0,5}钱|改要.{0,5}(钱|补偿)|用钱代替|换.{0,5}钱",
        r"丽迪雅.{0,20}(婚|私奔)|促成.{0,16}婚|替(?:他们|威克姆)(?:还|偿).{0,8}债|"
        r"(?:还|偿)(?:清|了|掉)?(?:威克姆|他们)(?:的)?债|"
        r"替丽迪雅.{0,25}(找|追|奔走|交涉|付钱)|为丽迪雅.{0,25}(找|追|奔走|交涉|付钱)",
    ),
    reasoning=(
        r"信",
        r"丽迪雅|行动|实际|亲自|出手|帮助|还债|偿债|买[^。！？]{0,6}(?:军职|委任)",
        r"改观|印象|看法|判断|动摇|重新审视|重新看|"
        r"偏见.{0,8}(?:消除|化解|消退)|(?:消除|化解).{0,8}偏见|敬意|感激|"
        r"为他[^。！？]{0,36}(?:骄傲|自豪)|为自己以前对他[^。！？]{0,18}(?:后悔|难过|惭愧)",
    ),
    avoid=(
        r"(?:坚持|要求).{0,16}(?:功劳|功绩).{0,12}(?:自己|本人).{0,6}(?:头上|名下)",
        r"(?:替|为)丽迪雅(?:还|偿)(?:了|清)?(?:她的)?债",
        r"(?:还|偿)(?:清|了|掉)?丽迪雅(?:的)?债",
    ),
)


def story_errors(answer, cited, *, facts, reasoning=(), avoid=()):
    claims = answer["claims"]
    text = "".join(claim["text"] for claim in claims)
    bad = []
    if answer["status"] != "answer" or not answer.get("prose") or answer.get("reader_note"):
        bad.append("未产出核全的段落")
    if not all(re.search(pattern, text) for pattern in facts):
        bad.append("缺少目标事实")
    if not all(re.search(pattern, text) for pattern in reasoning):
        bad.append("没有说明判断如何变化")
    if any(re.search(pattern, text) for pattern in avoid):
        bad.append("归因或确定程度与原文不符")
    if not cited:
        bad.append("没有可定位的原文依据")
    return bad


def data(response):
    response.raise_for_status()
    return response.json()


def check(client, pdf, *, through_reaction=False, recap_only=False):
    data(client.get("/api/library"))
    book = data(
        client.post(
            "/api/books", files={"file": ("傲慢与偏见.pdf", pdf, "application/pdf")}
        )
    )
    assert book["pages"] == 321 and len(book["chapters"]) == 61
    book_url = "/api/books/" + book["id"]
    thread_url = book_url + "/threads/" + book["threads"][0]["id"]
    checks = []

    def position(chapter):
        return book["chapters"][chapter - 1]["end"]

    def mark(pos):
        nonlocal book
        book = data(
            client.put(book_url + "/progress", json={"pos": pos, "rev": book["rev"]})
        )

    def ask(q):
        turn = data(
            client.post(thread_url + "/ask", json={"question": q, "rev": book["rev"]})
        )
        assert all(p <= book["progress"] for p in sources(turn)), "已读回答引用越界"
        return turn

    def sources(turn):
        return {
            ref["pos"] for claim in turn["answer"]["claims"] for ref in claim["refs"]
        }

    def chapters(turn):
        return {
            i
            for i, ch in enumerate(book["chapters"], 1)
            if any(ch["start"] <= p <= ch["end"] for p in sources(turn))
        }

    def body(turn):
        return "".join(claim["text"] for claim in turn["answer"]["claims"])

    def expect(label, passed, detail=""):
        checks.append(bool(passed))
        print("PASS" if passed else "FAIL", label, detail, flush=True)

    def story(turn, label, **rules):
        text = body(turn)
        cited = chapters(turn)
        bad = story_errors(turn["answer"], cited, **rules)
        expect(
            label,
            not bad,
            f"{len(text)} 字，引文来自 {sorted(cited)}，{'、'.join(bad) + '。' if bad else ''}{text}",
        )

    if recap_only:
        for page, bookmark in ((101, 2074), (131, 2681)):
            mark(bookmark)
            turn = ask("帮我回顾已读的情节")
            answer = turn["answer"]
            pages = {ref["page"] for claim in answer["claims"] for ref in claim["refs"]}
            expect(
                f"PDF 第 {page} 页已读回顾",
                answer["status"] == "answer" and answer.get("prose")
                and any(p <= 45 for p in pages)
                and any(45 < p < page - 20 for p in pages)
                and any(page - 20 <= p <= page for p in pages),
                f"引文页码 {sorted(pages)}\n{body(turn)}",
            )
        print(f"{sum(checks)}/2", flush=True)
        return all(checks)

    mark(position(16))
    first = ask("威克姆最初是怎么向伊丽莎白解释他和达西的过节的？")
    expect(
        "第 16 章，区分威克姆的说法",
        first["answer"]["status"] == "answer"
        and 16 in chapters(first)
        and re.search("牧师|俸禄|圣职|神职", body(first)),
    )

    mark(3415)
    early = ask(LETTER)
    expect(
        "还未读到解释，给出确认且不提前作答",
        early["pending"]
        and not re.search(r"三千|3,?000|three thousand", body(early), re.I),
    )
    mark(3429)
    letter = ask(LETTER)
    expect(
        "推进到来信中的解释后直接回答",
        letter["answer"]["status"] == "answer"
        and not letter["pending"]
        and 35 in chapters(letter)
        and re.search("三千|3,?000", body(letter)),
    )

    mark(position(36))
    story(ask(REACTION), "第 36 章，解释改变而且逐段取证", **REACTION_RULES)
    if through_reaction:
        print(f"{sum(checks)}/4", flush=True)
        return len(checks) == 4 and all(checks)
    mark(book["last"])
    arc = ask(
        "读完全书后，从威克姆起初的说法到达西的信，再到达西对丽迪雅的帮助，哪些事实让伊丽莎白改变对达西的印象？像聊天一样给我讲明白。"
    )
    story(
        arc,
        "全书，交代三段情节如何改变判断",
        **ARC_RULES,
    )
    ref = next(iter(sources(arc)), None)
    image = book_url + f"/turns/{arc['id']}/citation/{ref}"
    expect(
        "整段回答的原文可定位",
        ref is not None
        and client.get(image, params={"rev": book["rev"]}).headers.get("content-type")
        == "image/png",
    )

    for topic in (
        "班纳特太太为什么提起尼日斐花园？",
        "谁先去拜访宾利？",
        "宾利在哪座舞会上出场？",
        "舞会上宾利和简跳过舞吗？",
        "达西在舞会上怎样评价伊丽莎白？",
        "夏洛特怎样看达西的骄傲？",
        "开头班纳特太太有几个女儿？",
    ):
        ask(f"只讲到第 5 章，简答。{topic}")
    data(client.get(thread_url))
    resumed = ask(EARLIER)
    expect(
        "七轮别的话题后回到最早一问的原句",
        bool(sources(resumed)) and sources(resumed) == sources(first),
        f"引文来自 {sorted(chapters(resumed))}",
    )

    mark(position(16))
    history = data(client.get(thread_url))
    hidden = {turn["id"] for turn in history if turn.get("hidden")}
    expect(
        "回退后晚期对话、晚期原文都收起",
        arc["id"] in hidden
        and resumed["id"] in hidden
        and (
            ref is None
            or client.get(image, params={"rev": book["rev"]}).status_code == 404
        ),
    )
    again = ask(EARLIER)
    expect(
        "回退后只能找回仍然可见的旧话题",
        bool(sources(again)) and sources(again) == sources(first),
    )
    print(f"{sum(checks)}/9", flush=True)
    return len(checks) == 9 and all(checks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="Bookstacks 321 页英文版《傲慢与偏见》")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--through-reaction", action="store_true", help="只跑至第 36 章反应题")
    mode.add_argument("--recap-only", action="store_true", help="只复测 PDF 第 101 和 131 页的已读回顾")
    parser.add_argument("--trace", action="store_true", help="输出按请求关联的分阶段日志")
    args = parser.parse_args()
    pdf = args.pdf.read_bytes()
    if hashlib.sha256(pdf).hexdigest() != EDITION:
        parser.error("PDF 版本与固定书签不符，请使用 README 中的版本。")
    with tempfile.TemporaryDirectory() as root:
        os.environ["XUDU_DATA"] = root
        from backend.app import app
        from backend.usage import logger

        if args.trace:
            logging.basicConfig(level=logging.WARNING, format="%(message)s")
            logger.setLevel(logging.INFO)

        with TestClient(app, base_url="http://localhost") as client:
            try:
                passed = check(client, pdf, through_reaction=args.through_reaction, recap_only=args.recap_only)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                print("MODEL_ERROR" if status in (429, 502, 503) else "ERROR", status, flush=True)
                print("流程中断，依赖此轮的后续检查未执行。", flush=True)
                raise SystemExit(2) from None
            raise SystemExit(0 if passed else 1)
