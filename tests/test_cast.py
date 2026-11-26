import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import agent, cast, storage


class CastReview(unittest.TestCase):
    def test_ambiguous_titles_and_groups_do_not_identify_an_individual(self):
        for label in ("龙王", "众僧", "两个丫鬟", "马、赵、温、关"):
            self.assertFalse(cast.distinct_label(label))
        for label in ("东海龙王敖广", "牛魔王", "Miss Taylor"):
            self.assertTrue(cast.distinct_label(label))

    def test_directory_includes_secondary_characters_and_respects_rewind(self):
        names = [f"人物{i:02}" for i in range(1, 22)]
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(storage, "DATA", Path(folder)),
        ):
            storage.setup()
            with storage.db() as c:
                c.executemany(
                    "INSERT INTO sentences VALUES('sample',?,?,?, '[]')",
                    [
                        (
                            i,
                            1,
                            f"{name}来到车站。"
                            + ("熟人也叫他阿和。" if i == 2 else ""),
                        )
                        for i, name in enumerate(names, 1)
                    ],
                )
            read = []

            async def model(messages, **opts):
                body = messages[-1]["content"]
                if "从给定正文" in messages[0]["content"]:
                    ids = [int(line.split(" ", 1)[0]) for line in body.splitlines()]
                    read.extend(ids)
                    people = [[names[i - 1], i, "在车站露面。"] for i in ids]
                    links = []
                    if 2 in ids:
                        people.append(["阿和", 2, "在车站露面。"])
                        links = [["人物02", "阿和", 2], ["人物02", "未读的名字", 2]]
                    result = dict(people=people, same=links)
                elif "accepted" in messages[0]["content"]:
                    result = dict(accepted=list(range(len(json.loads(body)))))
                else:
                    result = {
                        "paragraphs": [
                            {"text": "人物01到了车站。", "quotes": [[1, "来到车站"]]},
                            {"text": "他后来到了终点。", "quotes": [[1, "到了终点"]]},
                            {"text": "他走了。", "quotes": [[21, "来到车站"]]},
                        ]
                    }
                return {"content": json.dumps(result)}

            async def verify(question, claims, *args, **kwargs):
                return claims, "", False

            with (
                patch.object(agent, "call", side_effect=model),
                patch.object(agent, "verify", side_effect=verify),
            ):
                for attempt, limit in enumerate((20, 20, 7)):
                    read.clear()
                    args = (
                        {"doc": "sample", "last": 21, "title": "测试书"},
                        "梳理已出场的人物",
                        limit,
                        0,
                        [],
                    )
                    result = asyncio.run(agent.respond(*args))
                    if attempt == 1:
                        self.assertEqual(read, [])
                    roster = result["claims"][result["cast_start"] :]
                    self.assertEqual(len(roster), limit)
                    self.assertEqual(result["cast_start"], 2)
                    self.assertEqual(result["claims"][1]["text"], "人物01到了车站。")
                    self.assertNotIn("终点", str(result))
                    self.assertIn("阿和", roster[1]["text"])
                    self.assertNotIn("未读的名字", str(result))
                    self.assertTrue(
                        all(
                            r["pos"] <= limit
                            for claim in result["claims"]
                            for r in claim["refs"]
                        )
                    )
                self.assertEqual(read, list(range(1, 8)))

    def test_named_follow_up_uses_the_displayed_directory_order(self):
        history = [
            {
                "q": "梳理已出场的人物",
                "a": [],
                "pos": 4,
                "scope_end": 4,
                "answer": {
                    "status": "answer",
                    "cast_start": 1,
                    "claims": [{"text": "介绍"}, {"name": "甲"}, {"name": "乙"}],
                },
            }
        ]
        with patch.object(
            agent, "respond_from_book", return_value={"status": "answer", "claims": []}
        ) as respond:
            asyncio.run(
                agent.respond({"last": 10}, "展开讲讲名单里的第二位", 7, 0, history)
            )
            args = respond.await_args.args
            self.assertIn("乙", args[1])
            self.assertEqual(args[2], 4)

    def test_discussion_preserves_the_models_verified_explanation(self):
        ref = {"pos": 1, "page": 1, "text": "他想再见她一面。"}
        draft = {
            "status": "answer",
            "claims": [{"text": "他还想再见她一面。", "ids": [1]}],
        }
        with (
            patch.object(
                agent,
                "phrase_for_reader",
                side_effect=AssertionError("不要把解释压成短句"),
            ),
            patch.object(
                agent,
                "verify",
                return_value=(
                    [{"text": "他还想再见她一面。", "refs": [ref]}],
                    "",
                    False,
                ),
            ),
        ):
            answer = asyncio.run(
                agent.assess_read_answer(
                    draft,
                    "为什么他留下了？",
                    {"last": 1},
                    1,
                    [ref],
                    {1: ref},
                    [],
                    False,
                    lambda _: None,
                )
            )
        self.assertEqual(answer["claims"][0]["text"], draft["claims"][0]["text"])
