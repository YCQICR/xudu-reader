import hashlib
import json
import re
import tempfile
from pathlib import Path

import pymupdf
from fastapi import HTTPException

from .storage import DATA, db

END = set("。！？!?")
CLOSE = set("”’」』")
CHAPTER = re.compile(r"^第[零〇○一二三四五六七八九十百千两0-9０-９]+[回章卷节]")
ENGLISH_HEADING = re.compile(r"^(?:Chapter|Volume)\s+[IVXLCDM\d]+$", re.I)
ENGLISH_FOLIO = re.compile(r"^(?:\d+\s*)?Chapter\s+[IVXLCDM]+(?:\s*\d+)?$", re.I)
ABBREVIATION = re.compile(r"\b(?:Mrs?|Ms|Dr|St|Jr|Sr|Rev|Hon|[A-Z])\.$")


def split_block(lines, page):
    width, height = page.rect.width, page.rect.height
    chars, boxes = [], []
    finished = False
    for line in lines:
        current_box = None
        for span in line["spans"]:
            for char in span["chars"]:
                ch = char["c"]
                # Keep decimal points and initials inside a word together.
                if (
                    finished
                    and chars
                    and chars[-1] in (".", "’")
                    and ch.isascii()
                    and ch.isalnum()
                ):
                    finished = False
                if finished and ch not in CLOSE and not ch.isspace():
                    value = "".join(chars).strip()
                    if value:
                        yield value, boxes
                    chars, boxes, current_box = [], [], None
                    finished = False
                coords = char["bbox"]
                if page.rotation:
                    coords = pymupdf.Rect(coords) * page.rotation_matrix
                if current_box is None:
                    current_box = [
                        coords[0] / width,
                        coords[1] / height,
                        coords[2] / width,
                        coords[3] / height,
                    ]
                    boxes.append(current_box)
                else:
                    current_box[0] = min(current_box[0], coords[0] / width)
                    current_box[1] = min(current_box[1], coords[1] / height)
                    current_box[2] = max(current_box[2], coords[2] / width)
                    current_box[3] = max(current_box[3], coords[3] / height)
                chars.append(ch)
                if ch in END or ch in CLOSE:
                    finished = True
                elif ch == ".":
                    finished = ABBREVIATION.search("".join(chars[-12:])) is None
        if chars and chars[-1].isascii() and chars[-1].isalnum():
            chars.append(" ")
    value = "".join(chars).strip()
    if value:
        yield value, boxes


def import_pdf(raw):
    digest = hashlib.sha256(raw).hexdigest()
    with db() as c:
        if c.execute("SELECT 1 FROM documents WHERE digest=?", (digest,)).fetchone():
            return digest
    try:
        pdf = pymupdf.open(stream=raw, filetype="pdf")
    except Exception as exc:
        raise HTTPException(422, "文件无法作为 PDF 打开。") from exc
    with pdf:
        if pdf.needs_pass:
            raise HTTPException(422, "请上传已解锁的 PDF。")
        if not 1 <= len(pdf) <= 2000:
            raise HTTPException(422, "请选择 1 至 2000 页的电子书。")
        toc = [
            (name, page)
            for level, name, page in pdf.get_toc()
            if level == 1 and 0 < page <= len(pdf)
        ]
        starts = sorted(set(page for _, page in toc))
        labels = {}
        for name, page_number in toc:
            labels.setdefault(page_number, []).append(name)
        rows, chapters = [], []
        first_body = 1
        if starts:
            title = labels[starts[0]][0]
            if CHAPTER.match(title) or ENGLISH_HEADING.fullmatch(title):
                first_body = starts[0]
        for number, page in enumerate(pdf, 1):
            if number < first_body:
                continue
            blocks = page.get_text("rawdict", flags=0, sort=True)["blocks"]
            start = len(rows) + 1
            page_text = page.get_text().splitlines()
            if any(
                re.fullmatch(r"目\s*[录錄]|(?:table of )?contents", line.strip(), re.I)
                for line in page_text[:4]
            ):
                continue
            if (
                len(page.get_links()) > 4
                and sum(
                    bool(re.match(r"^第.+[回章]|^chapter ", line.strip(), re.I))
                    for line in page_text
                )
                > 3
            ):
                continue
            page_chapters = labels.get(number, [])
            for block in blocks:
                if "lines" not in block:
                    continue
                value = "".join(
                    ch["c"]
                    for ln in block["lines"]
                    for sp in ln["spans"]
                    for ch in sp["chars"]
                ).strip()
                # Running folios can repeat a chapter title on every page.
                if block["bbox"][3] < page.rect.height * 0.06 and (
                    CHAPTER.match(value)
                    or ENGLISH_FOLIO.fullmatch(value)
                    or value.isdigit()
                ):
                    continue
                if (not toc and len(value) < 55 and CHAPTER.match(value)) or (
                    len(page_chapters) > 1
                    and value.replace(" ", "")
                    in {name.replace(" ", "") for name in page_chapters}
                ):
                    chapters.append(dict(page=number, start=len(rows) + 1, title=value))
                for sentence, boxes in split_block(block["lines"], page):
                    if sentence.isdigit() and (
                        block["bbox"][1] > page.rect.height * 0.91
                        or block["bbox"][3] < page.rect.height * 0.08
                    ):
                        continue
                    rows.append(
                        (
                            digest,
                            len(rows) + 1,
                            number,
                            sentence,
                            json.dumps(
                                [[round(v, 5) for v in b] for b in boxes],
                                separators=(",", ":"),
                            ),
                        )
                    )
            if len(page_chapters) == 1 and len(rows) >= start:
                chapters.append(dict(page=number, start=start, title=page_chapters[0]))
        if not rows:
            raise HTTPException(
                422, "没有提取到正文。请上传可以选中文字的 PDF，扫描版需要先做 OCR。"
            )
        for index, chapter in enumerate(chapters):
            chapter["end"] = (
                chapters[index + 1]["start"] - 1
                if index + 1 < len(chapters)
                else len(rows)
            )
            chapter["label"] = (
                f"第 {index + 1} 回"
                if CHAPTER.match(chapter["title"]) and "回" in chapter["title"][:8]
                else f"第 {index + 1} 章"
            )
        target = DATA / (digest + ".pdf")
        with tempfile.NamedTemporaryFile(dir=DATA, delete=False) as f:
            f.write(raw)
            tmp = Path(f.name)
        try:
            with db() as c:
                c.execute("BEGIN IMMEDIATE")
                if not c.execute(
                    "SELECT 1 FROM documents WHERE digest=?", (digest,)
                ).fetchone():
                    tmp.replace(target)
                    c.executemany("INSERT INTO sentences VALUES (?,?,?,?,?)", rows)
                    c.execute(
                        "INSERT INTO documents VALUES(?,?,?,?)",
                        (
                            digest,
                            len(pdf),
                            json.dumps(chapters, ensure_ascii=False),
                            len(rows),
                        ),
                    )
        finally:
            tmp.unlink(missing_ok=True)
    return digest
