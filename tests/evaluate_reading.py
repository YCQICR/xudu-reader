"""Run public sample questions through RelayCore in a temporary database."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from fastapi import HTTPException

CASES = json.loads(Path(__file__).with_name("reading_cases.json").read_text())


def select_cases(numbers, total):
    if total < 1:
        raise ValueError("评测题集为空。")
    invalid = sorted({number for number in numbers if not 1 <= number <= total})
    if invalid:
        raise ValueError(f"无效题号 {invalid}。有效范围为 1 至 {total}。")
    return sorted(set(numbers)) or list(range(1, total + 1))


async def evaluate(ids):
    from backend import agent
    from backend.importer import import_pdf
    from backend.storage import SAMPLE, db, setup

    setup()
    doc = import_pdf(SAMPLE.read_bytes())
    with db() as c:
        book = dict(
            c.execute(
                "SELECT digest AS doc,last FROM documents WHERE digest=?", (doc,)
            ).fetchone()
        )
    book["title"] = "西游记"
    semaphore = asyncio.Semaphore(3)

    async def one(i, case):
        async with semaphore:
            started = time.monotonic()
            status, reason = "PASS", ""
            try:
                a = await agent.respond(
                    book,
                    case["q"],
                    case["at"],
                    0,
                    case.get("history", []),
                    allow=case.get("allow", False),
                )
                refs = {r["pos"] for claim in a["claims"] for r in claim["refs"]}
                prose = (
                    " ".join(cl["text"] for cl in a["claims"]) + " " + a["background"]
                )
                visible = prose + " ".join(
                    r["text"] for cl in a["claims"] for r in cl["refs"]
                )
                assert a["status"] in case.get("status", ["answer"]), a["status"]
                if "min_facts" in case:
                    assert (
                        len(a["claims"]) >= case["min_facts"]
                        and len(refs) >= case["min_facts"]
                    ), "未分别给出多个有据事例"
                if "max_facts" in case:
                    assert len(a["claims"]) <= case["max_facts"], "超过读者要求的例子数"
                assert not any(t in prose for t in case.get("avoid_prose", [])), (
                    "复述旁枝或沿用原文腔调"
                )
                assert not case.get("expect") or any(
                    t in prose for t in case["expect"]
                ), "回答未包含目标事实"
                assert not any(t in visible for t in case.get("avoid", [])), (
                    "越界或过度展开"
                )
                assert not case["evidence"] or refs.intersection(case["evidence"]), (
                    "未引用目标证据"
                )
                assert case.get("allow") or all(p <= case["at"] for p in refs), (
                    "引用越界"
                )
                print(
                    f"PASS {i} {case['q']} {sorted(refs)}"
                    + (
                        f" {prose.strip()}"
                        if "avoid_prose" in case or "min_facts" in case
                        else ""
                    ),
                    flush=True,
                )
            except AssertionError as e:
                status, reason = "FAIL", str(e)
                print(
                    f"FAIL {i} {case['q']} {e} {locals().get('prose', '')} {sorted(locals().get('refs', []))}",
                    flush=True,
                )
            except HTTPException as e:
                status = "MODEL_ERROR" if e.status_code in (429, 502, 503) else "ERROR"
                reason = f"HTTP {e.status_code} {e.detail}"
                print(status, i, reason, flush=True)
            except Exception as e:
                status, reason = "ERROR", type(e).__name__
                print(status, i, reason, flush=True)
            return dict(
                id=i,
                status=status,
                reason=reason,
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )

    results = await asyncio.gather(
        *(one(i, CASES[i - 1]) for i in ids)
    )
    counts = {name: sum(r["status"] == name for r in results) for name in (
        "PASS", "FAIL", "MODEL_ERROR", "ERROR",
    )}
    print(f"{counts['PASS']}/{len(ids)} · " + " ".join(f"{k}={v}" for k, v in counts.items()), flush=True)
    return dict(book_digest=doc, provider="RelayCore", model=agent.relaycore_model(),
                selected=ids, counts=counts, results=results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("numbers", nargs="*", type=int, help="题号，省略时运行全部")
    parser.add_argument("--json-out", type=Path, help="保存每题结果和耗时")
    args = parser.parse_args()
    try:
        ids = select_cases(args.numbers, len(CASES))
    except ValueError as exc:
        parser.error(str(exc))
    with tempfile.TemporaryDirectory() as root:
        os.environ["XUDU_DATA"] = root
        result = asyncio.run(evaluate(ids))
    if args.json_out:
        args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if result["counts"]["MODEL_ERROR"] or result["counts"]["ERROR"]:
        sys.exit(2)
    sys.exit(0 if result["counts"]["PASS"] == len(ids) else 1)
