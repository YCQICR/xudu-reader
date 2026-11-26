import asyncio
import tempfile
import json
import threading
import time
from uuid import uuid4
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pymupdf
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import app as service, importer, storage, agent


class ModelConnection(unittest.IsolatedAsyncioTestCase):
    async def test_relaycore_uses_flash_with_its_own_key(self):
        endpoint = "https://relaycore.cn/v1/chat/completions"
        success = httpx.Response(
            200,
            request=httpx.Request("POST", endpoint),
            json={"choices": [{"message": {"content": "{}"}}]},
        )
        with patch.dict(
            agent.os.environ,
            {
                "RELAYCORE_API_KEY": "relay-key",
                "DEEPSEEK_API_KEY": "official-key",
                "DEEPSEEK_MODEL": "deepseek-v4-pro",
            },
            clear=True,
        ), patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=success)) as post:
            await agent.call([], agent.TOOLS)
            await agent.call([])
        tool_request, final_request = post.await_args_list
        for request in (tool_request, final_request):
            self.assertEqual(request.args, (endpoint,))
            self.assertEqual(request.kwargs["headers"]["Authorization"], "Bearer relay-key")
            self.assertEqual(request.kwargs["json"]["model"], "deepseek-v4.1-flash-1017")
            self.assertEqual(request.kwargs["json"]["thinking"], {"type": "disabled"})
            self.assertNotIn("reasoning_effort", request.kwargs["json"])
        self.assertEqual(tool_request.kwargs["json"]["tools"], agent.TOOLS)
        self.assertEqual(final_request.kwargs["json"]["response_format"], {"type": "json_object"})

    async def test_relaycore_retries_an_empty_json_reply_once(self):
        endpoint = "https://relaycore.cn/v1/chat/completions"
        responses = [
            httpx.Response(
                200,
                request=httpx.Request("POST", endpoint),
                json={"choices": [{"message": {"content": content}}]},
            )
            for content in (" ", '{"ok": true}')
        ]
        with patch.dict(
            agent.os.environ, {"RELAYCORE_API_KEY": "relay-key"}, clear=True,
        ), patch.object(
            httpx.AsyncClient, "post", AsyncMock(side_effect=responses)
        ) as post, patch.object(agent.usage, "charge") as charge:
            self.assertEqual(agent.unpack(await agent.call([])), {"ok": True})
        self.assertEqual(post.await_count, 2)
        self.assertEqual(charge.call_count, 2)
        self.assertEqual(post.await_args_list[0].kwargs["json"]["messages"], [])
        self.assertIn("JSON", post.await_args.kwargs["json"]["messages"][-1]["content"])

    async def test_legacy_official_key_cannot_replace_relaycore_key(self):
        for base_url in ("", "https://relaycore.cn/v1"):
            with self.subTest(base_url=base_url), patch.dict(
                agent.os.environ,
                {"RELAYCORE_BASE_URL": base_url, "DEEPSEEK_API_KEY": "official-key"},
                clear=True,
            ), patch.object(httpx.AsyncClient, "post", AsyncMock()) as post:
                with self.assertRaisesRegex(HTTPException, "RELAYCORE_API_KEY"):
                    await agent.call([])
                post.assert_not_awaited()

    async def test_relaycore_rejects_other_endpoints_before_sending(self):
        for base_url in (
            "https://api.deepseek.com/v1",
            "https://relaycore.cn.other.example/v1",
            "http://relaycore.cn/v1",
            "https://relaycore.cn:8080/v1",
            "https://relaycore.cn/v1?target=other",
        ):
            with self.subTest(base_url=base_url), patch.dict(
                agent.os.environ, {"RELAYCORE_API_KEY": "test-key", "RELAYCORE_BASE_URL": base_url}, clear=True,
            ), patch.object(httpx.AsyncClient, "post", AsyncMock()) as post:
                with self.assertRaisesRegex(HTTPException, "RELAYCORE_BASE_URL"):
                    await agent.call([])
                post.assert_not_awaited()

    async def test_relaycore_does_not_follow_gateway_redirects(self):
        destinations = []

        async def redirect(request):
            destinations.append(str(request.url))
            return httpx.Response(307, headers={"Location": "https://api.deepseek.com/chat/completions"})

        client = httpx.AsyncClient
        with patch.dict(
            agent.os.environ,
            {"RELAYCORE_API_KEY": "test-key", "RELAYCORE_BASE_URL": "https://api-cf.relaycore.cn/v1/"},
            clear=True,
        ), patch.object(httpx, "AsyncClient", side_effect=lambda **options: client(
            transport=httpx.MockTransport(redirect), **options
        )):
            with self.assertRaisesRegex(HTTPException, "HTTP 307"):
                await agent.call([])
        self.assertEqual(destinations, ["https://api-cf.relaycore.cn/v1/chat/completions"])

    async def test_relaycore_uses_model_override_and_reports_gateway_status(self):
        rejected = httpx.Response(
            404,
            request=httpx.Request("POST", "https://relaycore.cn/v1/chat/completions"),
            json={"error": {"type": "model_not_found"}},
        )
        with patch.dict(
            agent.os.environ,
            {"RELAYCORE_API_KEY": "test-key", "RELAYCORE_MODEL": "deepseek-v4.1-flash",
             "RELAYCORE_REASONING_EFFORT": "high"},
            clear=True,
        ), patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=rejected)) as post:
            with self.assertRaisesRegex(HTTPException, "RelayCore 的密钥分组不支持当前模型"):
                await agent.call([])
        self.assertEqual(post.await_args.kwargs["json"]["model"], "deepseek-v4.1-flash")
        self.assertEqual(post.await_args.kwargs["json"]["reasoning_effort"], "high")

    async def test_relaycore_http_success_without_a_model_answer_is_an_error(self):
        upstream_error = httpx.Response(
            200,
            request=httpx.Request("POST", "https://relaycore.cn/v1/chat/completions"),
            json={"error": {"message": "upstream unavailable"}},
        )
        with patch.dict(agent.os.environ, {"RELAYCORE_API_KEY": "test-key"}, clear=True), patch.object(
            httpx.AsyncClient, "post", AsyncMock(return_value=upstream_error)
        ):
            with self.assertRaisesRegex(HTTPException, "未返回模型回答"):
                await agent.call([])

    async def test_retries_connection_errors_before_sending(self):
        failure = httpx.ConnectError("temporary connection failure")
        success = httpx.Response(
            200,
            request=httpx.Request("POST", "https://relaycore.cn/v1/chat/completions"),
            json={"choices": [{"message": {"content": "{}"}}]},
        )
        with patch.dict(agent.os.environ, {"RELAYCORE_API_KEY": "test-key"}, clear=True), patch.object(
            httpx.AsyncClient, "post", AsyncMock(side_effect=[failure, success])
        ) as post, patch.object(agent.asyncio, "sleep", AsyncMock()), patch.object(
            agent.usage, "charge"
        ) as charge:
            self.assertEqual(await agent.call([]), {"content": "{}"})
        self.assertEqual(post.await_count, 2)
        self.assertEqual(post.await_args.args, ("https://relaycore.cn/v1/chat/completions",))
        charge.assert_called_once()


class ReadingBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = cls.enterClassContext(tempfile.TemporaryDirectory())
        for module in (storage, importer, service):
            cls.enterClassContext(patch.object(module, "DATA", Path(root)))
        cls.client = cls.enterClassContext(TestClient(service.app))
        cls.client.get("/api/library")

    def setUp(self):
        # Endpoint unit tests supply their own generation/audit replies. Retrieval
        # behavior is exercised separately below and against the real model.
        self.plan = self.enterContext(
            patch.object(
                agent, "plan_reading", return_value={"scope": "local", "groups": []}
            )
        )
        c = self.client
        c.cookies.clear()
        b = c.get("/api/library").json()["book"]
        self.path = "/api/books/" + b["id"]
        self.thread = self.path + "/threads/" + b["threads"][0]["id"]
        self.b = b
        rows = c.get(self.path + "/pages/11").json()["rows"]
        self.low = next(row["pos"] for row in rows if "我进去，我进去" in row["text"])
        self.high = next(row["pos"] for row in rows if "原来是座铁板桥" in row["text"])

    def mark(self, pos):
        response = self.client.put(
            self.path + "/progress", json={"rev": self.b["rev"], "pos": pos}
        )
        self.assertEqual(response.status_code, 200)
        self.b = response.json()

    def ask(self):
        return self.client.post(
            self.thread + "/ask",
            json={"question": "读到哪里了？", "rev": self.b["rev"]},
        )

    def cards(self):
        res = self.client.get(self.path + "/cards", params={"rev": self.b["rev"]})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def until_card(self, state):
        for _ in range(120):
            cards = self.cards()
            if cards and cards[0]["state"] == state:
                return cards[0]
            time.sleep(0.025)
        self.fail("追踪卡未进入状态 " + state)

    def start_card(self):
        self.mark(self.low)
        initial = {
            "status": "limited",
            "claims": [{"text": "石猴自告奋勇。", "refs": [self.b["location"]]}],
            "background": "",
        }
        with patch.object(agent, "respond", return_value=initial):
            turn = self.ask().json()["id"]
        path = self.path + "/turns/" + turn
        with patch.object(
            agent,
            "respond",
            return_value={**initial, "claims": [{"text": "仅授权的秘密", "refs": []}]},
        ):
            self.client.post(
                path + "/choice", json={"rev": self.b["rev"], "action": "allow"}
            ).raise_for_status()
        for _ in range(2):
            self.client.post(
                path + "/track", json={"rev": self.b["rev"]}
            ).raise_for_status()
        self.assertEqual(len(self.cards()), 1)
        self.assertNotIn("仅授权", str(self.cards()))
        return turn

    def test_tracking_versions_and_rollback(self):
        turn = self.start_card()

        async def updates(messages):
            body = json.loads(messages[-1]["content"])
            if "new" in body:
                self.assertTrue(
                    all(self.low < row["pos"] <= self.high + 1 for row in body["new"])
                )
                new = (
                    [
                        {"text": "看见了铁板桥。", "ids": [str(self.high)] * 2},
                        {"text": "尚未读到的情节。", "ids": [str(self.high + 1)]},
                        {"text": "不是句子编号。", "ids": [True, "1e2", "x"]},
                        {"text": "编号字段类型错误。", "ids": None},
                    ]
                    if body["new"][-1]["pos"] == self.high
                    else []
                )
                return {"content": json.dumps({"claims": new})}
            self.assertEqual([c["text"] for c in body["claims"]], ["看见了铁板桥。"])
            return {"content": '{"accepted":["0"]}'}

        with patch.object(agent, "call", side_effect=updates):
            self.mark(self.high)
            card = self.until_card("ready")
            self.assertEqual(len(card["versions"]), 2)
            self.assertEqual(
                [ref["pos"] for ref in card["versions"][-1]["claims"][0]["refs"]],
                [self.high],
            )
            self.mark(self.high + 1)
            card = self.until_card("ready")
            self.assertEqual(card["checked"], self.high + 1)
            self.assertEqual(len(card["versions"]), 2)
            self.mark(self.low)
            self.assertNotIn("铁板桥", str(self.cards()))
            self.mark(self.low - 1)
            self.assertEqual(self.cards(), [])
            self.mark(self.high)
            self.assertEqual(self.until_card("ready")["id"], turn)
        self.client.cookies.clear()
        self.client.get("/api/library")
        self.assertEqual(
            self.client.get(
                self.path + "/cards", params={"rev": self.b["rev"]}
            ).status_code,
            404,
        )

    def save_question(self, question):
        answer = dict(
            status="answer",
            claims=[dict(text="看见一座铁板桥。", refs=[self.b["location"]])],
            background="",
        )
        with patch.object(agent, "respond", return_value=answer):
            response = self.client.post(
                self.thread + "/ask", json={"question": question, "rev": self.b["rev"]}
            )
        response.raise_for_status()
        return response.json()["id"]

    def track_question(self, turn):
        response = self.client.post(
            self.path + "/turns/" + turn + "/track", json={"rev": self.b["rev"]}
        )
        response.raise_for_status()
        return response.json()

    def test_group_questions_and_sidebar_deletion(self):
        self.mark(self.high)
        first = self.save_question("石猴跳进瀑布后看见了什么？")
        original = self.track_question(first)
        second = self.save_question("请问石猴跳进瀑布后看到了什么呢？")
        with patch.object(
            agent, "call", side_effect=AssertionError("同题无需调用模型")
        ):
            self.assertEqual(self.track_question(second)["id"], original["id"])
        third = self.save_question("石猴穿过水幕后，眼前出现了什么？")
        fourth = self.save_question("石猴为什么跳进瀑布？")
        with patch.object(
            agent,
            "call",
            side_effect=[{"content": '{"match":1}'}, {"content": '{"match":null}'}],
        ):
            self.assertEqual(self.track_question(third)["id"], first)
            self.assertNotEqual(self.track_question(fourth)["id"], first)
        self.assertEqual(len(self.cards()), 2)
        params = {"rev": self.b["rev"]}
        self.client.delete(
            self.path + "/cards/" + first, params=params
        ).raise_for_status()
        self.assertEqual(len(self.client.get(self.thread).json()), 4)
        self.assertEqual([c["id"] for c in self.cards()], [fourth])
        self.client.delete(self.thread, params=params).raise_for_status()
        self.assertEqual(self.client.get(self.thread).status_code, 404)
        self.assertEqual(self.cards()[0]["id"], fourth)
        self.assertEqual(self.client.get("/api/library").json()["book"]["threads"], [])
        replacement = self.client.post(self.path + "/threads").json()["threads"]
        self.assertEqual(len(replacement), 1)
        self.assertEqual(
            self.client.post(self.path + "/threads").json()["threads"], replacement
        )
        self.client.delete(self.path).raise_for_status()
        self.assertEqual(
            self.client.get("/api/library").json(),
            {
                "books": [],
                "book": None,
                "username": None,
                "invitation_required": False,
                "demo": False,
            },
        )
        with storage.db() as c:
            for table in ("card_links", "tracked_questions", "card_checks"):
                self.assertFalse(
                    c.execute(
                        f"SELECT 1 FROM {table} WHERE turn=?", (fourth,)
                    ).fetchone()
                )
            self.assertFalse(
                c.execute("SELECT 1 FROM turns WHERE id=?", (fourth,)).fetchone()
            )
        self.client.post(
            "/api/books",
            files={
                "file": ("西游记.pdf", storage.SAMPLE.read_bytes(), "application/pdf")
            },
        ).raise_for_status()
        self.assertEqual(self.client.get("/api/library").json()["book"]["progress"], 0)

    def test_tracking_same_question_concurrently(self):
        self.mark(self.high)
        q = "石猴跳进瀑布时看见了什么？"
        ids = [self.save_question(q) for _ in range(2)]
        barrier = threading.Barrier(2)
        results = []

        def track(turn):
            barrier.wait(timeout=5)
            results.append(self.track_question(turn)["id"])

        workers = [threading.Thread(target=track, args=(turn,)) for turn in ids]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(8)
        self.assertEqual(len(results), 2)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(self.cards()), 1)

    def test_legacy_card_links_preserve_evidence_and_ownership(self):
        from backend import topics

        self.mark(self.high)
        ids = [self.save_question("石猴看见了什么？") for _ in range(2)]
        with storage.db() as c:
            for i, turn in enumerate(ids):
                c.execute(
                    "INSERT INTO tracked_questions VALUES(?,?,?)",
                    (turn, self.b["id"], i),
                )
                evidence = [dict(text=f"旧版事实{i}", refs=[self.b["location"]])]
                c.execute(
                    "INSERT INTO card_checks VALUES(?,?,NULL,?)",
                    (turn, self.high, json.dumps(evidence)),
                )
        topics.migrate()
        topics.migrate()
        cards = self.cards()
        self.assertEqual([c["id"] for c in cards], [ids[0]])
        self.assertEqual(len(cards[0]["versions"][0]["claims"]), 2)
        self.mark(self.low)
        self.assertEqual(self.cards(), [])
        self.client.cookies.clear()
        self.client.get("/api/library")
        params = {"rev": self.b["rev"]}
        for route in (self.thread, self.path + "/cards/" + ids[0], self.path):
            self.assertEqual(self.client.delete(route, params=params).status_code, 404)

    def test_rephrase_uses_original_facts_and_checks_evidence(self):
        self.mark(self.high)
        turn = self.save_question("石猴看到了什么？")
        path = self.path + "/turns/" + turn + "/rephrase"
        self.mark(self.high + 10)
        with patch.object(
            agent,
            "call",
            side_effect=[
                {
                    "content": '{"lines":[{"text":"他看见了一座铁板桥。","from":[1]},{"text":"无依据","from":[20]}]}'
                },
                {"content": '{"accepted":[0]}'},
            ],
        ) as model:
            updated = self.client.post(path, json={"rev": self.b["rev"]})
            updated.raise_for_status()
            claims = updated.json()["answer"]["claims"]
            self.assertEqual([c["text"] for c in claims], ["他看见了一座铁板桥。"])
            self.assertEqual([r["pos"] for r in claims[0]["refs"]], [self.high])
            self.assertEqual(
                self.client.post(path, json={"rev": self.b["rev"]}).json(),
                updated.json(),
            )
            self.assertEqual(model.call_count, 2)
        self.mark(self.low)
        self.assertEqual(
            self.client.post(path, json={"rev": self.b["rev"]}).status_code, 404
        )
        self.assertEqual(
            self.client.get(self.thread).json(), [{"id": turn, "hidden": True}]
        )

    def test_tracking_retry_and_late_completion(self):
        turn = self.start_card()
        retry = self.path + "/cards/" + turn + "/retry"
        started, release = threading.Event(), threading.Event()

        async def slow(*args, **kwargs):
            started.set()
            await asyncio.to_thread(release.wait, 5)
            return {"content": '{"claims":[]}'}

        with patch.object(
            agent, "call", side_effect=HTTPException(502, "模拟暂时不可用")
        ):
            self.mark(self.high)
            self.assertEqual(self.until_card("failed")["checked"], self.low)
        with patch.object(agent, "call", side_effect=slow):
            self.client.post(retry, json={"rev": self.b["rev"]}).raise_for_status()
            try:
                self.assertTrue(started.wait(3))
                self.mark(self.low)
                self.assertEqual(self.until_card("ready")["checked"], self.low)
            finally:
                release.set()
            self.mark(self.high)
            self.assertEqual(self.until_card("ready")["checked"], self.high)

    def test_decline_then_repeat_gets_a_fresh_scoped_consent(self):
        self.mark(self.low)
        with patch.object(
            agent,
            "respond",
            return_value={"status": "limited", "claims": [], "background": ""},
        ) as model:
            first = self.ask().json()
            self.client.post(
                self.path + "/turns/" + first["id"] + "/choice",
                json={"action": "decline", "rev": self.b["rev"]},
            ).raise_for_status()
            again = self.ask().json()
            self.assertTrue(again["pending"])
            self.assertNotEqual(first["id"], again["id"])
            self.assertEqual(model.call_count, 1)

    def test_reader_name_cannot_survive_in_a_partial_paraphrase(self):
        self.mark(self.low)
        draft = {
            "status": "limited",
            "claims": [{"text": "孙悟空应声表示要跃入瀑布", "ids": [self.low]}],
            "background": "尚未得名",
        }
        with patch.object(
            agent,
            "call",
            side_effect=[
                {"content": json.dumps(draft)},
                {"content": '{"accepted":[0],"needs_later":true}'},
            ],
        ):
            t = self.client.post(
                self.thread + "/ask",
                json={"question": "孙悟空的结局是什么？", "rev": self.b["rev"]},
            ).json()
        self.assertTrue(t["pending"])
        self.assertIn("我进去", str(t["answer"]))
        self.assertNotIn("孙悟空", str(t["answer"]))
        self.assertEqual(t["answer"]["background"], "")

    def test_failed_audit_is_not_a_spoiler_offer(self):
        self.mark(self.low)
        draft = {
            "status": "answer",
            "claims": [{"text": "问题里的名字是他的真名", "ids": [self.low]}],
            "background": "尚未得名",
        }
        with patch.object(
            agent,
            "call",
            side_effect=[
                {"content": json.dumps(draft)},
                {"content": '{"lines":[{"text":"问题里的名字是他的真名","from":[1]}]}'},
                {
                    "content": '{"accepted":[],"background_ok":false,"needs_later":false}'
                },
            ],
        ):
            t = self.ask().json()
        self.assertEqual(t["answer"]["status"], "unknown")
        self.assertFalse(t["answer"]["claims"] or t["answer"]["background"])
        self.assertIn("依据", t["answer"]["reader_note"])
        self.assertFalse(t["pending"])

    def test_reader_note_uses_the_original_bookmark_in_old_and_new_turns(self):
        self.mark(self.low)
        old_format = {
            "status": "limited",
            "claims": [{"text": "旧回答", "refs": [self.b["location"]]}],
            "background": "",
        }
        with patch.object(agent, "respond", return_value=old_format):
            turn = self.client.post(
                self.thread + "/ask",
                json={"question": "石猴跳进瀑布后看见了什么？", "rev": self.b["rev"]},
            ).json()
        note = turn["answer"]["reader_note"]
        self.assertIn("我进去，我进去", note)
        self.assertIn("没交代", note)
        self.assertIn("看见了什么", note)
        self.assertNotIn("铁板桥", note)
        self.mark(self.high)
        self.assertEqual(
            self.client.get(self.thread).json()[0]["answer"]["reader_note"], note
        )
        with patch.object(
            agent,
            "respond",
            return_value={"status": "answer", "claims": [], "background": "词义解释"},
        ):
            self.assertNotIn("reader_note", self.ask().json()["answer"])
        self.mark(0)
        self.assertTrue(all(t["hidden"] for t in self.client.get(self.thread).json()))
        with patch.object(
            agent,
            "respond",
            return_value={"status": "unknown", "claims": [], "background": ""},
        ):
            note = self.ask().json()["answer"]["reader_note"]
        self.assertIn("没有标记", note)

    def test_explicit_scope_choice_does_not_depend_on_reviewer_guess(self):
        self.mark(self.low)
        for question in ("孙悟空的结局是什么？", "石猴跳进瀑布后看见了什么？"):
            with patch.object(
                agent,
                "call",
                return_value={"content": '{"status":"unknown","claims":[]}'},
            ):
                turn = self.client.post(
                    self.thread + "/ask",
                    json={"question": question, "rev": self.b["rev"]},
                ).json()
            self.assertTrue(turn["pending"])
            self.assertEqual(turn["answer"]["claims"], [])

    def test_unfounded_motive_does_not_offer_consent(self):
        self.mark(self.low)
        with patch.object(
            agent,
            "call",
            side_effect=[
                {"content": '{"status":"limited","claims":[]}'},
                {"content": '{"accepted":[],"needs_later":false}'},
            ],
        ):
            self.assertFalse(self.ask().json()["pending"])

    def test_retrospective_looks_back_and_clips_context(self):
        plan = {
            "subjects": ["悟空"],
            "groups": [["变做", "摇身一变"], ["学成", "学会"]],
        }
        with storage.db() as c:
            doc = c.execute(
                "SELECT doc FROM shelves WHERE id=?", (self.b["id"],)
            ).fetchone()[0]
        for at in (self.low, 582):
            rows = agent.recall_passages(doc, at, plan)
            self.assertTrue(all(r["pos"] <= at for r in rows))
            if at == 582:
                self.assertTrue(any("变做一棵松树" in r["text"] for r in rows))
            else:
                self.assertNotIn("松树", str(rows))

    def test_discussion_saves_prose_only_after_every_sentence_passes_review(self):
        self.mark(self.high)
        self.plan.return_value = {"scope": "local", "style": "discussion"}
        question = "石猴为什么能进洞？"
        first = {"text": "众猴在找敢进瀑布的人。", "ids": [self.low - 1]}
        second = {"text": "石猴主动请缨。", "ids": [self.low]}
        narrative = [
            {"text": "石猴是自己争取到这次机会的。", "ids": [self.low]},
            {"text": "众猴正在找敢进瀑布的人，他主动站了出来。", "ids": [self.low - 1, self.low]},
        ]

        for accepted, expected, prose in (
            ([0, 1], narrative, True),
            ([0], [first, second], False),
        ):
            with self.subTest(accepted=accepted), patch.object(
                agent,
                "call",
                side_effect=[
                    {"content": json.dumps({"status": "answer", "claims": [first, second]})},
                    {"content": '{"accepted":[0,1]}'},
                ] + [
                    {"content": json.dumps({"sentences": narrative})},
                    {"content": json.dumps({"accepted": accepted})},
                ] * (1 if prose else 2),
            ):
                turn = self.client.post(
                    self.thread + "/ask", json={"rev": self.b["rev"], "question": question}
                )
                turn.raise_for_status()
                answer = turn.json()["answer"]
                self.assertEqual(answer["prose"], prose)
                self.assertEqual(
                    [c["text"] for c in answer["claims"]],
                    [c["text"] for c in expected],
                )
                self.assertTrue(all(
                    ref["pos"] <= self.high for claim in answer["claims"] for ref in claim["refs"]
                ))

    def test_each_requested_part_respects_the_bookmark_and_chapter(self):
        with storage.db() as c:
            doc = c.execute(
                "SELECT doc FROM shelves WHERE id=?", (self.b["id"],)
            ).fetchone()[0]
        parts = [
            {"topic": "瀑布前", "terms": ["我进去"], "chapter": 1},
            {"topic": "瀑布内", "terms": ["铁板桥"], "chapter": 1},
            {"topic": "后来", "terms": ["师父"], "chapter": 2},
        ]
        before = agent.grouped_evidence({"doc": doc}, self.low, parts)
        after = agent.grouped_evidence({"doc": doc}, self.high, parts)
        self.assertTrue(before[0]["passages"])
        self.assertEqual(before[1]["passages"], [])
        self.assertTrue(any("铁板桥" in row["text"] for row in after[1]["passages"]))
        self.assertEqual(after[2]["passages"], [])
        self.assertTrue(all(row["pos"] <= self.high for group in after for row in group["passages"]))

    def test_first_event_retrieval_keeps_early_source(self):
        with storage.db() as c:
            doc = c.execute(
                "SELECT doc FROM shelves WHERE id=?", (self.b["id"],)
            ).fetchone()[0]
        parts = [
            {"topic": "最初提到洞", "terms": ["洞"], "order": "earliest"},
            {"topic": "读到附近的洞", "terms": ["洞"]},
        ]
        early, nearby = agent.grouped_evidence({"doc": doc}, 1600, parts)
        self.assertTrue(early["passages"] and nearby["passages"])
        self.assertLess(
            max(row["pos"] for row in early["passages"]),
            min(row["pos"] for row in nearby["passages"]),
        )

    def test_grouped_retrieval_reuses_only_in_scope_older_citations(self):
        with storage.db() as c:
            doc = c.execute(
                "SELECT doc FROM shelves WHERE id=?", (self.b["id"],)
            ).fetchone()[0]
        parts = [
            {"topic": "起初的说法", "terms": []},
            {"topic": "之后的所见", "terms": []},
        ]
        first, second = agent.grouped_evidence(
            {"doc": doc}, self.low, parts, (self.low, self.high)
        )
        self.assertEqual(first["prior_refs"], [self.low])
        self.assertTrue(all(row["pos"] <= self.low for row in first["passages"]))
        self.assertEqual(second["passages"], [])

    def test_grouped_retrieval_reuses_evidence_from_a_matching_later_topic(self):
        with storage.db() as c:
            doc = c.execute(
                "SELECT doc FROM shelves WHERE id=?", (self.b["id"],)
            ).fetchone()[0]
        parts = [
            {"topic": "石猴怎么请缨", "terms": []},
            {"topic": "石猴怎样发现铁板桥", "terms": []},
        ]
        history = [("石猴怎样发现铁板桥？", (self.low, self.high))]
        first, second = agent.grouped_evidence(
            {"doc": doc}, self.low, parts, recent_answers=history
        )
        self.assertEqual(first["passages"], [])
        self.assertEqual(second["prior_refs"], [self.low])
        self.assertTrue(all(row["pos"] <= self.low for row in second["passages"]))

    def test_summary_retries_missing_parts_and_discloses_remaining_gap(self):
        self.mark(self.high)
        self.plan.return_value = {
            "scope": "local", "style": "discussion",
            "parts": [
                {"topic": "谁先提出进去", "terms": ["我进去"], "chapter": 1},
                {"topic": "进去看见什么", "terms": ["铁板桥"], "chapter": 1},
            ],
        }
        first = {"text": "石猴主动请缨。", "ids": [self.low]}
        second = {"text": "他认出一座铁板桥。", "ids": [self.high]}

        async def model(messages, tools=None, **options):
            payload = json.loads(messages[-1]["content"])
            content = messages[0]["content"]
            if "verified" in payload:
                if "revision" in payload:
                    self.assertEqual(payload["revision"]["missing"], ["进去看见什么"])
                response = {"sentences": [first]}
            elif "claims" in payload:
                response = {"accepted": list(range(len(payload["claims"])))}
                if payload["status"] == "synthesis":
                    response.update(coverage=[
                        {"topic": 0, "claim": 0},
                        {"topic": 1, "claim": 99},
                    ])
            else:
                self.assertIn("requested_parts", payload)
                response = {"status": "answer", "claims": [first, second]}
            self.assertIn("JSON", content)
            return {"content": json.dumps(response, ensure_ascii=False)}

        with patch.object(agent, "call", side_effect=model) as call:
            turn = self.client.post(
                self.thread + "/ask",
                json={"question": "谁先说要进去，进去之后看到什么？", "rev": self.b["rev"]},
            )
            turn.raise_for_status()
        answer = turn.json()["answer"]
        self.assertEqual(call.call_count, 6)
        self.assertEqual([cl["text"] for cl in answer["claims"]], [first["text"]])
        self.assertIn("没找到足够的依据", answer["reader_note"])

    def test_failed_grouped_synthesis_displays_short_source_excerpts(self):
        self.mark(self.high)
        self.plan.return_value = {
            "scope": "local",
            "style": "discussion",
            "parts": [
                {"topic": "谁先提出进去", "terms": ["我进去"], "chapter": 1},
                {"topic": "进去看见什么", "terms": ["铁板桥"], "chapter": 1},
            ],
        }
        request_id = str(uuid4())
        long_draft = {"text": "不宜显示的初稿" * 60, "ids": [self.low, self.high]}
        outputs = [
            {"status": "answer", "claims": [long_draft]},
            {"accepted": [0]},
            {"sentences": [{"text": "无效引文", "ids": [self.high + 99999]}]},
            {"sentences": [{"text": "无效引文", "ids": [self.high + 99999]}]},
        ]
        with patch.object(
            agent, "call", side_effect=[{"content": json.dumps(item)} for item in outputs]
        ), self.assertLogs("uvicorn.error.deepseek", level="INFO") as trace:
            response = self.client.post(
                self.thread + "/ask",
                json={
                    "question": "谁先说要进去，进去之后看到什么？",
                    "rev": self.b["rev"],
                    "request_id": request_id,
                },
            )
        response.raise_for_status()
        answer = response.json()["answer"]
        self.assertFalse(answer["prose"])
        self.assertIn("没找到足够的依据", answer["reader_note"])
        self.assertEqual(
            [ref["pos"] for claim in answer["claims"] for ref in claim["refs"]],
            [self.low, self.high],
        )
        body = "".join(claim["text"] for claim in answer["claims"])
        self.assertLessEqual(len(body), 260)
        self.assertNotIn("不宜显示的初稿", body)
        self.assertTrue(all(f"request={request_id}" in entry for entry in trace.output))
        self.assertTrue(any("stage=synthesis_fallback" in entry for entry in trace.output))

    def test_grouped_fallback_keeps_audited_details_if_lead_is_rejected(self):
        first, second = (
            {"text": text, "refs": [{"pos": pos, "page": 11, "text": text}]}
            for pos, text in (
                (self.low, "石猴自告奋勇。"), (self.high, "石猴看见铁板桥。")
            )
        )
        groups = [
            {"topic": topic, "passages": cl["refs"]}
            for topic, cl in (("谁请缨", first), ("看见什么", second))
        ]
        proposal = {
            "sentences": [
                {"text": "他的来信逐条推翻了对方的说法。", "ids": [self.low]},
                {"text": first["text"], "ids": [self.low]},
                {"text": second["text"], "ids": [self.high]},
            ],
        }
        audit = {"accepted": [0, 1], "coverage": [{"topic": 0, "claim": 1}]}
        with patch.object(
            agent, "call",
            side_effect=[{"content": json.dumps(item)} for item in (proposal, audit)] * 2,
        ):
            selected, missing = asyncio.run(
                agent.compose_answer("谁请缨，后来看到什么？", [first, second], [], groups)
            )
        self.assertEqual(selected, [first])
        self.assertEqual(missing, [2])

    def test_grouped_synthesis_accepts_long_verified_explanation_without_rewriting(self):
        first, second = (
            {"pos": pos, "page": 11, "text": value}
            for pos, value in ((self.low, "石猴主动请缨。"), (self.high, "石猴看见铁板桥。"))
        )
        claims = [{"text": ref["text"], "refs": [ref]} for ref in (first, second)]
        groups = [{"topic": ref["text"], "passages": [ref]} for ref in (first, second)]
        long_text = (
            "这件事可以分两处来看。众猴正需要有人试探瀑布，石猴听见以后就主动请缨。"
            "愿意往里跳是他自己的选择，这一点也是这一段里他的行动和其他猴子不同的地方。"
            "进去以后他看见了铁板桥，这交代了另一件事，也就是他进去以后实际发现了什么。"
        ) * 3
        response = {"sentences": [
            {"text": long_text, "ids": [self.low, self.high]},
            {"text": "先有请缨，才有进洞后的发现。", "ids": [self.low, self.high], "paragraph_start": True},
        ]}
        audit = {"accepted": [0, 1], "coverage": [
            {"topic": 0, "claim": 0}, {"topic": 1, "claim": 0},
        ]}
        with patch.object(
            agent, "call", side_effect=[{"content": json.dumps(item)} for item in (response, audit)]
        ) as model:
            answer, missing = asyncio.run(
                agent.compose_answer("谁先进去，他后来又看到了什么？", claims, [], groups)
            )
        self.assertIsNone(missing)
        self.assertGreater(len(long_text), 300)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(answer[0]["text"], long_text)
        self.assertEqual(answer[0]["refs"], [first, second])
        self.assertTrue(answer[1]["paragraph_start"])

    def test_grouped_synthesis_keeps_sentence_citations_and_rejects_wrong_stage(self):
        lead, early, late = (
            {"pos": pos, "page": 11, "text": value}
            for pos, value in (
                (self.low - 1, "众猴请有胆量者进去。"),
                (self.low, "石猴主动请缨。"),
                (self.high, "结果尚待核对。"),
            )
        )
        claims = [
            {"text": row["text"], "refs": [row]} for row in (lead, early, late)
        ]
        groups = [
            {"topic": "谁先进去", "passages": [early]},
            {"topic": "后来看到什么", "passages": [late]},
        ]
        draft = {"sentences": [
            {"text": lead["text"], "ids": [lead["pos"]]},
            {"text": early["text"], "ids": [early["pos"]], "stage": 1},
            {"text": late["text"], "ids": [late["pos"]], "stage": 2},
        ]}

        async def model(messages, **options):
            payload = json.loads(messages[-1]["content"])
            if "claims" in payload:
                first = payload["claims"][0]
                self.assertEqual(first["claim_index"], 0)
                self.assertEqual(first["refs"], [lead])
                result = {"accepted": [0, 1], "coverage": [
                    {"topic": 0, "claim": 1}, {"topic": 1, "claim": 1},
                ]}
            else:
                result = draft
            return {"content": json.dumps(result)}

        with patch.object(agent, "call", side_effect=model):
            selected, missing = asyncio.run(
                agent.compose_answer("谁先进去，又看到了什么？", claims, [], groups)
            )
        self.assertEqual(selected, claims[:2])
        self.assertEqual(missing, [2])

    def test_old_topic_evidence_stays_in_same_thread_and_bookmark(self):
        self.mark(self.low)
        early_answer = {
            "status": "answer", "claims": [{"text": "石猴请缨。", "refs": [self.b["location"]]}],
            "background": "", "scope_end": self.low,
        }
        original = "谁先应下众猴的约定？"
        with patch.object(agent, "respond", return_value=early_answer):
            old = self.client.post(
                self.thread + "/ask", json={"question": original, "rev": self.b["rev"]}
            ).json()
        self.mark(self.high)
        for n in range(7):
            self.save_question(f"独立的问题 {n}")
        q = "回到一开始问的约定，给我看原句。"

        async def pick(question, entries):
            return next((h for h in entries if h["q"] == original), None)

        with patch.object(agent, "choose_history_anchor", side_effect=pick):
            response = self.client.post(
                self.thread + "/ask", json={"question": q, "rev": self.b["rev"]}
            )
            response.raise_for_status()
            a = response.json()["answer"]
            self.assertEqual(a["anchor_question"], original)
            self.assertEqual(a["scope_end"], self.low)
            self.assertTrue(all(r["pos"] <= self.low for cl in a["claims"] for r in cl["refs"]))
            self.assertNotIn("_reference_turn", a)

            self.mark(self.low - 1)
            result = self.client.get(self.thread).json()
            self.assertTrue(next(t for t in result if t["id"] == old["id"])["hidden"])
            response = self.client.post(
                self.thread + "/ask", json={"question": q, "rev": self.b["rev"]}
            )
            response.raise_for_status()
            self.assertEqual(response.json()["answer"]["status"], "clarify")

    def test_unfamiliar_phrasing_retries_after_empty_first_attempt(self):
        self.mark(582)
        q = "那位还压着什么绝活儿？"
        self.plan.side_effect = [
            {"scope": "local"},
            {"scope": "overview", "groups": [["变化", "学成"]], "subjects": ["悟空"]},
        ]

        async def respond(messages, tools=None):
            if tools:
                return {"content": '{"status":"unknown","claims":[]}'}
            payload = json.loads(messages[-1]["content"])
            if "evidence" in payload:
                for r in payload["evidence"]:
                    self.assertLessEqual(r["pos"], 582)
                self.assertIn(547, [r["pos"] for r in payload["evidence"]])
                return {
                    "content": '{"status":"answer","claims":[{"text":"他学成了七十二般变化。","ids":[547]},{"text":"越界事实","ids":[583]}]}'
                }
            self.assertEqual(len(payload["claims"]), 1)
            return {"content": '{"accepted":[0]}'}

        with patch.object(agent, "call", side_effect=respond):
            answer = self.client.post(
                self.thread + "/ask", json={"rev": self.b["rev"], "question": q}
            ).json()["answer"]
        self.assertEqual(
            [c["text"] for c in answer["claims"]], ["他学成了七十二般变化。"]
        )
        self.assertEqual(self.plan.call_count, 2)

    def test_future_name_cannot_attribute_a_read_characters_actions(self):
        self.mark(self.low)
        self.plan.return_value = {
            "scope": "overview",
            "person": "孙悟空",
            "subjects": ["孙悟空", "悟空"],
        }
        with patch.object(
            agent, "call", side_effect=AssertionError("未建立的身份不能归给当前人物")
        ):
            out = self.client.post(
                self.thread + "/ask",
                json={"rev": self.b["rev"], "question": "孙悟空有什么绝活？"},
            ).json()
        self.assertEqual(out["answer"]["status"], "unknown")
        self.assertIn("称呼", out["answer"]["reader_note"])
        self.assertFalse(out["answer"]["claims"] or out["pending"])

    def test_nearest_restatement_is_selected_inside_bookmark(self):
        with storage.db() as c:
            doc = c.execute(
                "SELECT doc FROM shelves WHERE id=?", (self.b["id"],)
            ).fetchone()[0]
            refs = storage.texts(c, doc, 822, 822)
        anchors, rows = agent.repeated_passages(doc, 1080, refs, "latest")
        self.assertIn(889, anchors)
        self.assertTrue(all(0 < p <= 1080 for p in rows))
        before, _ = agent.repeated_passages(doc, 888, refs, "latest")
        self.assertNotIn(889, before)
        answer = dict(
            status="answer",
            claims=[dict(text=refs[0]["text"], refs=refs)],
            background="",
        )
        with patch.object(
            agent,
            "call",
            side_effect=[
                {
                    "content": json.dumps(
                        {
                            "claims": [
                                dict(position=889, text=rows[889]["text"], ids=[889]),
                                dict(position=1081, text="超前", ids=[1081]),
                            ]
                        }
                    )
                },
                {"content": '{"accepted":[0]}'},
            ],
        ):
            corrected = asyncio.run(
                agent.check_reading_order(
                    dict(doc=doc),
                    1080,
                    "离书签最近的一次重述？",
                    answer,
                    "latest",
                    [],
                    lambda _: None,
                )
            )
        self.assertEqual([r["pos"] for r in corrected["claims"][0]["refs"]], [889])

    def test_tally_returns_verified_distinct_items_and_respects_chapter(self):
        self.mark(20231)
        self.plan.return_value = {
            "scope": "tally",
            "unit": "位",
            "through_chapter": 1,
            "groups": [],
        }
        with storage.db() as c:
            pos = json.loads(
                c.execute(
                    "SELECT chapters FROM documents WHERE digest=(SELECT doc FROM shelves WHERE id=?)",
                    (self.b["id"],),
                ).fetchone()[0]
            )[0]["end"]

        async def reply(messages, tools=None):
            data = json.loads(messages[-1]["content"])
            if tools:
                self.assertEqual(data["allowed_max"], pos)
                self.assertTrue(all(r["pos"] <= pos for r in data["recent_evidence"]))
                candidates = [
                    ("甲", pos),
                    ("甲", pos),
                    ("另一个称谓", pos - 1),
                    ("乙", pos - 2),
                    ("后文", pos + 1),
                ]
                return {
                    "content": json.dumps(
                        {
                            "status": "answer",
                            "tally_kind": "items",
                            "claims": [
                                dict(key=n, text=n, ids=[p]) for n, p in candidates
                            ],
                        }
                    )
                }
            self.assertEqual(data["status"], "tally_items")
            self.assertEqual(
                [x["text"] for x in data["claims"]], ["甲", "另一个称谓", "乙"]
            )
            return {"content": '{"accepted":[0,2]}'}

        with patch.object(agent, "call", side_effect=reply):
            turn = self.client.post(
                self.thread + "/ask",
                json={"rev": self.b["rev"], "question": "截至第一回一共遇见多少位？"},
            ).json()
        self.assertEqual([x["text"] for x in turn["answer"]["claims"]], ["甲", "乙"])
        self.assertIn("至少 2 位", turn["answer"]["reader_note"])
        self.assertEqual(
            self.client.get(self.thread).json()[-1]["answer"], turn["answer"]
        )

    def test_tally_explicit_total_requires_audited_evidence(self):
        draft = {
            "status": "answer",
            "tally_kind": "explicit",
            "claims": [{"text": "三位。", "ids": [3]}],
        }
        with patch.object(
            agent,
            "call",
            side_effect=[
                {"content": '{"accepted":[]}'},
                {"content": '{"accepted":[0]}'},
            ],
        ):
            args = (draft, {3: {"pos": 3, "text": "共三位。"}}, "共有几位？", [], {})
            self.assertIsNone(asyncio.run(agent.tally_answer(*args)))
            answer = asyncio.run(agent.tally_answer(*args))
            self.assertEqual(answer["claims"][0]["text"], "三位。")
            self.assertNotIn("reader_note", answer)

    def test_whole_prefix_review_visits_every_sentence_through_bookmark(self):
        self.mark(self.low)
        delivered = []
        self.plan.return_value = {"scope": "prefix_review"}

        async def model(messages, **options):
            body = json.loads(messages[-1]["content"])
            if "passage" in body:
                ids = [
                    int(line.split(" ", 1)[0]) for line in body["passage"].splitlines()
                ]
                delivered.extend(ids)
                return {"content": json.dumps({"ids": [str(ids[0]), ids[-1]]})}
            if "evidence" in body:
                visible = [row["pos"] for row in body["evidence"]]
                self.assertIn(1, visible)
                self.assertIn(self.low, visible)
                self.assertLessEqual(max(visible), self.low)
                return {
                    "content": json.dumps(
                        {"claims": [{"text": "石猴自告奋勇。", "ids": [self.low]}]}
                    )
                }
            return {"content": '{"accepted":[0]}'}

        blocks = agent.review_blocks
        with (
            patch.object(agent, "call", side_effect=model),
            patch.object(
                agent,
                "review_blocks",
                side_effect=lambda rows: blocks(rows, budget=300),
            ),
        ):
            for question in ("帮我回顾已读的情节", "从开篇到这里发生了什么？"):
                delivered.clear()
                turn = self.client.post(
                    self.thread + "/ask",
                    json={"question": question, "rev": self.b["rev"]},
                )
                turn.raise_for_status()
                self.assertEqual(sorted(delivered), list(range(1, self.low + 1)))
                answer = turn.json()["answer"]
                self.assertEqual(answer["status"], "answer")
                self.assertEqual(answer["claims"][0]["refs"][0]["pos"], self.low)
        self.plan.assert_awaited_once()

    def test_prefix_review_rejects_invalid_and_out_of_batch_positions(self):
        row = {"pos": 1, "page": 1, "text": "测试原文。"}
        with patch.object(agent, "texts", return_value=[row]):
            for invalid in (True, "未知", "2", None):
                with self.subTest(invalid=invalid), patch.object(
                    agent, "call", return_value={"content": json.dumps({"ids": [invalid]})}
                ):
                    with self.assertRaises(HTTPException) as failure:
                        asyncio.run(agent.review_prefix(
                            {"doc": "unused"}, "回顾已读情节", 1, [], lambda _: None
                        ))
                    self.assertEqual(failure.exception.status_code, 502)

    def test_long_recap_keeps_verified_sections_when_rewrite_only_covers_the_opening(self):
        rows = [{"pos": pos, "page": (pos - 1) // 8 + 1, "text": f"情节 {pos}。"}
                for pos in range(1, 301)]
        delivered = []

        async def model(messages, **options):
            payload = json.loads(messages[-1]["content"])
            if "passage" in payload:
                positions = [int(line.split(" ", 1)[0]) for line in payload["passage"].splitlines()]
                delivered.append(positions)
                return {"content": json.dumps({"claims": [
                    {"text": f"情节 {positions[-1]}。", "ids": [positions[-1]]},
                    {"text": "超过书签", "ids": [301]},
                ]})}
            if "verified" in payload:
                return {"content": '{"sentences":[{"text":"情节 90。","ids":[90]}]}'}
            approved = {"accepted": list(range(len(payload["claims"])))}
            if payload["status"] == "synthesis":
                approved["coverage"] = [{"topic": 0, "claim": 0}]
            return {"content": json.dumps(approved)}

        with patch.object(agent, "call", side_effect=model):
            result = asyncio.run(agent.recap_sections(
                "回顾已读情节", rows, [{"start": 91}, {"start": 214}], [], lambda _: None,
            ))
        self.assertEqual(sorted(pos for section in delivered for pos in section), list(range(1, 301)))
        self.assertEqual([claim["refs"][0]["pos"] for claim in result["claims"]], [90, 213, 300])
        self.assertTrue(all(claim["paragraph_start"] for claim in result["claims"]))
        self.assertNotIn("超过书签", str(result))

    def test_review_rewrites_unsupported_details_before_showing_a_character(self):
        self.mark(self.low)
        replies = [
            {"ids": [self.low]},
            {"claims": [{"text": "石猴自告奋勇，已经当上大王。", "ids": [self.low]}]},
            {"accepted": []},
            {"claims": [{"text": "石猴自告奋勇。", "ids": [self.low]}]},
            {"accepted": [0]},
        ]
        with patch.object(
            agent, "call", side_effect=[{"content": json.dumps(r)} for r in replies]
        ):
            response = self.client.post(
                self.thread + "/ask",
                json={"question": "帮我回顾已读的情节", "rev": self.b["rev"]},
            )
            response.raise_for_status()
            claims = response.json()["answer"]["claims"]
            self.assertEqual([cl["text"] for cl in claims], ["石猴自告奋勇。"])
            self.assertEqual(claims[0]["refs"][0]["pos"], self.low)

    def test_follow_up_can_open_previous_second_citation_after_reload(self):
        self.mark(self.high)
        earlier = self.b["location"]
        original = dict(
            status="answer",
            background="",
            claims=[
                dict(text="第一条", refs=[{**earlier, "pos": self.low}]),
                dict(text="第二条", refs=[earlier]),
            ],
        )
        with patch.object(agent, "respond", return_value=original):
            self.ask().raise_for_status()
        self.client.get(self.thread).raise_for_status()
        with patch.object(agent, "call", side_effect=AssertionError("不需要重新检索")):
            answer = self.client.post(
                self.thread + "/ask",
                json={"question": "刚才第二条的原句在哪一页？", "rev": self.b["rev"]},
            ).json()["answer"]
        self.assertEqual(answer["claims"][0]["refs"], [earlier])

    def test_ordinary_recap_is_clipped_before_planning_and_without_spoiler_offer(self):
        self.mark(20231)
        self.plan.return_value = {"scope": "local"}
        with patch.object(
            agent,
            "call",
            return_value={
                "content": '{"status":"limited","claims":[],"background":""}'
            },
        ) as call:
            turn = self.client.post(
                self.thread + "/ask",
                json={
                    "question": "只讲到第十回，别讲后文。接下来发生什么？",
                    "rev": self.b["rev"],
                },
            ).json()
        stop = self.b["chapters"][9]["end"]
        for passage in self.plan.call_args.args[3]:
            self.assertLessEqual(passage["pos"], stop)
        prompts = json.loads(call.call_args_list[0].args[0][-1]["content"])
        self.assertEqual(prompts["allowed_max"], stop)
        self.assertEqual(turn["answer"]["scope_end"], stop)
        self.assertFalse(turn["pending"])

    def test_tally_continues_from_persisted_checkpoint_and_counts_new_members(self):
        self.mark(20231)
        self.plan.return_value = {"scope": "tally", "unit": "个"}
        question = "到59回合为止，师徒取经路上遇见了多少妖怪？"

        async def reply(messages, tools=None):
            data = json.loads(messages[-1]["content"])
            if "status" in data:
                return {"content": '{"accepted":[0,1]}'}
            if "known" in data:
                self.assertEqual(len(data["known"]), 2)
                limit = max(r["pos"] for r in data["evidence"])
                name = "新增" if limit == 220 else "再新增"
                claims = [{"key": name, "text": name, "ids": [limit]}]
            else:
                pos = data["allowed_max"]
                claims = [
                    dict(key=name, text=name, ids=[pos]) for name in ("金角", "银角")
                ]
            return {
                "content": json.dumps(
                    dict(status="answer", tally_kind="items", claims=claims)
                )
            }

        with patch.object(agent, "call", side_effect=reply):

            def ask(q, continue_from=None):
                res = self.client.post(
                    self.thread + "/ask", json={
                        "question": q, "rev": self.b["rev"], "continue_from": continue_from,
                    }
                )
                res.raise_for_status()
                return res.json()

            source = ask(question)
            self.assertIn("至少 2 个", source["answer"]["reader_note"])
            one = ask("继续核对")["answer"]
            self.assertEqual(one["tally"]["checked"], 220)
            self.assertIn("累计确认 3 个", one["reader_note"])
        for index in range(7):
            self.save_question(f"独立问题 {index}")
        self.client.get(self.thread).raise_for_status()

        async def empty(messages):
            data = json.loads(messages[-1]["content"])
            self.assertEqual(data["question"], question)
            self.assertEqual(len(data["known"]), 3)
            self.assertEqual(max(r["pos"] for r in data["evidence"]), 440)
            return {"content": '{"status":"answer","tally_kind":"items","claims":[]}'}

        with patch.object(agent, "call", side_effect=empty):
            two = ask("继续核对")["answer"]
        self.assertEqual(two["tally"]["checked"], 440)
        self.assertIn("累计确认 3 个", two["reader_note"])
        with patch.object(agent, "call", side_effect=reply):
            from_source = ask("继续核对", source["id"])["answer"]
        self.assertEqual(from_source["tally"]["checked"], 220)
        self.assertEqual(from_source["scope_end"], 20231)
        self.mark(20230)
        hidden = self.client.post(
            self.thread + "/ask", json={
                "question": "继续核对", "rev": self.b["rev"], "continue_from": source["id"],
            }
        )
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(ask("继续核对")["answer"]["status"], "clarify")

    def test_card_filters_each_fact_by_reveal_position_within_batch(self):
        self.start_card()
        middle = self.low + 2

        async def updates(messages):
            data = json.loads(messages[-1]["content"])
            if "new" in data:
                old = data["prior"][0]["id"]
                return {
                    "content": json.dumps(
                        {
                            "claims": [
                                {"text": "先读到的事实", "ids": [middle]},
                                {
                                    "text": "后来订正",
                                    "ids": [self.high],
                                    "replaces": [old],
                                },
                            ]
                        }
                    )
                }
            return {"content": '{"accepted":[0,1]}'}

        with patch.object(agent, "call", side_effect=updates):
            self.mark(self.high)
            card = self.until_card("ready")
            self.assertEqual(
                [c["text"] for c in card["current_claims"]],
                ["先读到的事实", "后来订正"],
            )
            self.mark(middle)
            card = self.until_card("ready")
            self.assertEqual(
                [c["text"] for c in card["current_claims"]],
                [card["versions"][0]["claims"][0]["text"], "先读到的事实"],
            )
            self.assertNotIn("后来订正", str(card))

    def test_partial_examples_keep_the_supported_item(self):
        self.mark(self.high)
        self.plan.return_value = {"scope": "overview", "example_count": 2, "groups": []}
        draft = {
            "status": "answer",
            "claims": [
                {"text": "他看见铁板桥。", "ids": [self.high]},
                {"text": "又见到另一样东西。", "ids": [self.high]},
            ],
        }
        with patch.object(
            agent,
            "call",
            side_effect=[
                {"content": json.dumps(draft)},
                {"content": '{"accepted":[0]}'},
            ],
        ):
            reply = self.client.post(
                self.thread + "/ask",
                json={"rev": self.b["rev"], "question": "挑两个他看见的东西。"},
            ).json()
        self.assertEqual(reply["answer"]["status"], "answer")
        self.assertEqual(len(reply["answer"]["claims"]), 1)
        self.assertIn("确认 1 个", reply["answer"]["reader_note"])
        self.assertFalse(reply["pending"])

    def test_unresolved_referent_clarifies_before_retrieval(self):
        self.mark(self.low)
        self.plan.return_value = {"scope": "clarify"}
        with patch.object(
            agent, "call", side_effect=AssertionError("指代不清时先澄清")
        ):
            answer = self.ask().json()["answer"]
        self.assertEqual(answer["status"], "clarify")
        self.assertEqual(answer["claims"], [])

    def test_long_distance_recall_in_sample(self):
        cases = json.loads(Path(__file__).with_name("reading_cases.json").read_text())
        with storage.db() as c:
            doc = c.execute(
                "SELECT doc FROM shelves WHERE id=?", (self.b["id"],)
            ).fetchone()[0]
            for case in cases:
                if "terms" not in case:
                    continue
                with self.subTest(question=case["q"]):
                    rows = agent.search(
                        c, doc, case["at"], case["at"], case["terms"], case.get("order")
                    )
                    pos = {r["pos"] for r in rows}
                    self.assertTrue(pos.intersection(case["evidence"]))
                    self.assertTrue(all(p <= case["at"] for p in pos))

    def test_scanned_and_non_pdf(self):
        with pymupdf.open() as d:
            d.new_page()
            data = d.tobytes()
        for raw in (data, b"plain text"):
            resp = self.client.post(
                "/api/books", files={"file": ("test.pdf", raw, "application/pdf")}
            )
            self.assertEqual(resp.status_code, 422)

    def test_pdf_import_reports_mixed_pages_without_changing_sentence_positions(self):
        with pymupdf.open() as doc, pymupdf.open() as scan:
            doc.new_page().insert_text((72, 96), "The first readable page.")
            picture = scan.new_page(width=350, height=200)
            picture.insert_text((45, 72), "Text flattened into the scan.")
            doc.new_page().insert_image(
                pymupdf.Rect(72, 72, 422, 272), pixmap=picture.get_pixmap()
            )
            doc.new_page().insert_text((72, 96), "The final readable page.")
            pdf = doc.tobytes()
        for _ in range(2):
            uploaded = self.client.post(
                "/api/books", files={"file": ("mixed.pdf", pdf, "application/pdf")}
            )
            uploaded.raise_for_status()
            book = uploaded.json()
            self.assertEqual(book["pages"], 3)
            self.assertEqual(book["import_report"], {"readable_pages": 2, "missing_pages": [2]})
            self.assertEqual(book["progress"], 0)
            path = "/api/books/" + book["id"] + "/pages/"
            self.assertEqual(self.client.get(path + "2").json()["rows"], [])
            self.assertEqual(self.client.get(path + "3").json()["rows"][0]["pos"], 2)

    def test_guest_and_search_boundary(self):
        self.mark(self.low)
        self.assertEqual(
            self.client.get(self.path + "/search", params={"q": "铁板桥"}).json(), []
        )
        self.mark(self.high)
        self.assertTrue(
            self.client.get(self.path + "/search", params={"q": "铁板桥"}).json()
        )
        self.client.cookies.clear()
        self.client.get("/api/library")
        self.assertEqual(self.client.get(self.path + "/pages/11").status_code, 404)

    def test_quote_picker_only_returns_read_sentences(self):
        self.mark(self.low)
        c = self.client
        visible = c.get(self.path + "/pages/11", params={"read_only": True}).json()[
            "rows"
        ]
        self.assertEqual(visible[-1]["pos"], self.low)
        self.assertFalse(
            c.get(self.path + "/pages/12", params={"read_only": True}).json()["rows"]
        )
        self.assertGreater(
            c.get(self.path + "/pages/11").json()["rows"][-1]["pos"], self.low
        )
        self.assertEqual(c.get("/api/library").json()["book"]["progress"], self.low)

    def test_stream_retry_is_one_turn(self):
        self.mark(self.low)
        payload = {
            "question": "石猴做了什么？",
            "rev": self.b["rev"],
            "request_id": str(uuid4()),
        }

        async def respond(*args, report, **kwargs):
            report("正在查找已读原文…")
            report("正在核验回答和引文…")
            return {"status": "answer", "claims": [], "background": ""}

        with patch.object(service.agent, "respond", side_effect=respond) as model:

            def send():
                r = self.client.post(
                    self.thread + "/ask",
                    json=payload,
                    headers={"Accept": "application/x-ndjson"},
                )
                return [json.loads(line) for line in r.text.splitlines() if line]

            events = send()
            self.assertIn("status", events[0])
            self.assertEqual(events[-1]["result"]["id"], payload["request_id"])
            self.assertEqual(send()[-1], events[-1])
            self.assertEqual(model.call_count, 1)
        self.assertEqual(len(self.client.get(self.thread).json()), 1)

    def test_failed_generation_can_retry_without_duplicate_history(self):
        self.mark(self.low)
        payload = {
            "question": "石猴做了什么？",
            "rev": self.b["rev"],
            "request_id": str(uuid4()),
        }
        headers = {"Accept": "application/x-ndjson"}
        answer = {"status": "answer", "claims": [], "background": ""}
        with patch.object(
            service.agent,
            "respond",
            side_effect=[HTTPException(502, "模型暂时不可用。"), answer],
        ):
            failed = self.client.post(
                self.thread + "/ask", json=payload, headers=headers
            )
            self.assertEqual(
                json.loads(failed.text.splitlines()[-1])["error"], "模型暂时不可用。"
            )
            self.assertEqual(self.client.get(self.thread).json(), [])
            retried = self.client.post(
                self.thread + "/ask", json=payload, headers=headers
            )
            self.assertEqual(
                json.loads(retried.text.splitlines()[-1])["result"]["id"],
                payload["request_id"],
            )
            self.assertEqual(len(self.client.get(self.thread).json()), 1)

    def test_stop_ask_and_spoiler_generation(self):
        self.mark(self.low)
        limited = {"status": "limited", "claims": [], "background": ""}
        with patch.object(service.agent, "respond", return_value=limited):
            tid = self.ask().json()["id"]
        for path, args in [
            (self.thread + "/ask", {"question": "新问题"}),
            (self.path + "/turns/" + tid + "/choice", {"action": "allow"}),
        ]:
            rid = str(uuid4())
            started = threading.Event()

            async def waiting(*args, report, **kwargs):
                report("正在核验回答和引文…")
                started.set()
                await asyncio.sleep(30)
                return limited

            out = []
            with patch.object(service.agent, "respond", side_effect=waiting):
                runner = threading.Thread(
                    target=lambda: out.append(
                        self.client.post(
                            path,
                            json={**args, "rev": self.b["rev"], "request_id": rid},
                            headers={"Accept": "application/x-ndjson"},
                        )
                    )
                )
                runner.start()
                self.assertTrue(started.wait(5))
                self.assertTrue(
                    self.client.post("/api/requests/" + rid + "/stop").json()["stopped"]
                )
                runner.join(5)
                self.assertFalse(runner.is_alive())
                self.assertIn("已停止", out[0].text)
            history = self.client.get(self.thread).json()
            self.assertEqual(len(history), 1)
            self.assertTrue(history[0]["pending"])
            self.assertFalse(history[0]["spoiler"])

    def test_citation_crop_and_hidden_history(self):
        self.mark(self.high)
        answer = {
            "status": "answer",
            "claims": [{"text": "铁板桥。", "refs": [self.b["location"]]}],
            "background": "",
        }
        with patch.object(service.agent, "respond", return_value=answer):
            t = self.ask().json()
        url = self.path + "/turns/" + t["id"] + "/citation/"
        cut = self.client.get(url + str(self.high), params={"rev": self.b["rev"]})
        self.assertEqual(cut.status_code, 200)
        self.assertTrue(cut.content.startswith(b"\x89PNG"))
        self.assertEqual(
            self.client.get(
                url + str(self.high + 1), params={"rev": self.b["rev"]}
            ).status_code,
            404,
        )
        self.mark(self.low)
        self.assertEqual(
            self.client.get(self.thread).json(), [{"id": t["id"], "hidden": True}]
        )
        self.assertEqual(
            self.client.get(
                url + str(self.high), params={"rev": self.b["rev"]}
            ).status_code,
            404,
        )

    def test_late_answer_and_old_confirmation(self):
        self.mark(self.high)
        limited = {"status": "limited", "claims": [], "background": ""}
        with patch.object(service.agent, "respond", return_value=limited):
            t = self.ask().json()
        started, release = threading.Event(), threading.Event()

        async def slow(*args, **kwargs):
            started.set()
            await asyncio.to_thread(release.wait, 8)
            return limited

        response = []
        with patch.object(service.agent, "respond", side_effect=slow):
            worker = threading.Thread(target=lambda: response.append(self.ask()))
            worker.start()
            try:
                self.assertTrue(started.wait(8))
                self.mark(self.low)
            finally:
                release.set()
                worker.join(10)
        self.assertEqual(response[0].status_code, 409)
        self.assertEqual(
            self.client.post(
                self.path + "/turns/" + t["id"] + "/choice",
                json={"action": "allow", "rev": self.b["rev"]},
            ).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
