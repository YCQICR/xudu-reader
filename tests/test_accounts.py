import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import accounts, agent, app as service, importer, storage, usage

PASSWORD = "example-test-password"
SECOND_BOOK = Path(__file__).resolve().parents[1] / "samples" / "emma.pdf"


class PrivateShelf(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = cls.enterClassContext(tempfile.TemporaryDirectory())
        for module in (storage, importer, service):
            cls.enterClassContext(patch.object(module, "DATA", Path(root)))
        cls.browser = cls.enterClassContext(TestClient(service.app))

    def setUp(self):
        self.browser.cookies.clear()

    def call(self, http, route, method="GET", payload=None):
        response = http.request(method, route, json=payload)
        self.assertTrue(
            response.is_success, (route, response.status_code, response.text[:300])
        )
        return response.json()

    def test_existing_shelf_keeps_ids_and_private_citations_across_logins(self):
        browser = self.browser
        b = self.call(browser, "/api/library")["book"]
        guest = browser.cookies.get(accounts.GUEST_COOKIE)
        path = "/api/books/" + b["id"]
        thread = path + "/threads/" + b["threads"][0]["id"]
        b = self.call(browser, path + "/progress", "PUT", {"pos": 103, "rev": b["rev"]})
        answer = dict(
            status="answer",
            claims=[dict(text="一座铁板桥。", refs=[b["location"]])],
            background="",
        )
        with patch.object(agent, "respond", return_value=answer):
            t = self.call(
                browser,
                thread + "/ask",
                "POST",
                {"question": "看见了什么？", "rev": b["rev"]},
            )
        self.call(
            browser, path + "/turns/" + t["id"] + "/track", "POST", {"rev": b["rev"]}
        )
        credentials = {"username": "reader-one", "password": PASSWORD}
        self.call(browser, "/api/account/register", "POST", credentials)
        self.assertFalse(browser.cookies.get(accounts.GUEST_COOKIE))
        self.call(browser, "/api/account/logout", "POST")
        with TestClient(service.app) as other:
            self.call(other, "/api/account/login", "POST", credentials)
            lib = self.call(other, "/api/library")
            self.assertEqual(lib["book"]["id"], b["id"])
            self.assertEqual(lib["book"]["progress"], 103)
            self.assertEqual(self.call(other, thread)[0]["id"], t["id"])
            image = path + "/turns/" + t["id"] + "/citation/103"
            self.assertEqual(
                other.get(image, params={"rev": b["rev"]}).status_code, 200
            )
            self.assertEqual(
                len(self.call(other, path + "/cards?rev=" + str(b["rev"]))), 1
            )
            token = other.cookies.get(accounts.ACCOUNT_COOKIE)
            self.call(other, "/api/account/logout", "POST")
            self.assertEqual(
                other.get(thread, cookies={accounts.ACCOUNT_COOKIE: token}).status_code,
                401,
            )
        with TestClient(service.app) as stranger:
            self.call(stranger, "/api/library")
            self.call(
                stranger,
                "/api/account/register",
                "POST",
                {"username": "reader-two", "password": PASSWORD},
            )
            for route in (
                thread,
                path + "/pages/11",
                path + "/search?q=石猴",
                image + "?rev=" + str(b["rev"]),
                path + "/cards?rev=" + str(b["rev"]),
            ):
                self.assertEqual(stranger.get(route).status_code, 404, route)
        with storage.db() as c:
            self.assertIsNone(
                c.execute("SELECT 1 FROM visitors WHERE id=?", (guest,)).fetchone()
            )

    def test_sessions_open_different_books_and_stale_bookmark_cannot_overwrite(self):
        a = self.browser
        xy = self.call(a, "/api/library")["book"]
        creds = {"username": "reader-sync", "password": PASSWORD}
        self.call(a, "/api/account/register", "POST", creds)
        xy_path = "/api/books/" + xy["id"]
        with TestClient(service.app) as b:
            self.call(b, "/api/account/login", "POST", creds)
            with SECOND_BOOK.open("rb") as file:
                result = a.post(
                    "/api/books", files={"file": ("爱玛.pdf", file, "application/pdf")}
                )
                result.raise_for_status()
                emma = result.json()
            self.assertEqual(self.call(a, "/api/session")["book"], emma["id"])
            self.assertEqual(self.call(b, "/api/session")["book"], xy["id"])
            first = self.call(
                b, xy_path + "/progress", "PUT", {"pos": 103, "rev": xy["rev"]}
            )
            self.call(a, xy_path + "/open", "POST")
            stale = a.put(xy_path + "/progress", json={"pos": 98, "rev": xy["rev"]})
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(self.call(a, "/api/session")["rev"], first["rev"])
            second = self.call(
                a, xy_path + "/progress", "PUT", {"pos": 98, "rev": first["rev"]}
            )
            self.assertEqual(self.call(b, "/api/session")["rev"], second["rev"])
            thread = xy_path + "/threads/" + xy["threads"][0]["id"]
            with patch.object(
                agent,
                "respond",
                return_value=dict(status="limited", claims=[], background=""),
            ):
                turn = self.call(
                    a,
                    thread + "/ask",
                    "POST",
                    {"question": "后文如何？", "rev": second["rev"]},
                )
            self.assertTrue(turn["pending"])
            self.assertFalse(self.call(b, thread)[0]["pending"])
            choice = xy_path + "/turns/" + turn["id"] + "/choice"
            self.assertEqual(
                b.post(
                    choice, json={"rev": second["rev"], "action": "allow"}
                ).status_code,
                409,
            )
            self.call(a, "/api/books/" + emma["id"] + "/open", "POST")
            self.call(a, xy_path + "/open", "POST")
            self.assertEqual(
                a.post(
                    choice, json={"rev": second["rev"], "action": "allow"}
                ).status_code,
                409,
            )

    def test_demo_is_sample_only_and_caps_model_usage(self):
        with patch.dict(
            "os.environ",
            {
                "XUDU_DEMO": "1",
                "XUDU_DEMO_DAILY_CALLS": "2",
                "XUDU_DEMO_READER_CALLS": "1",
            },
        ):
            b = self.call(self.browser, "/api/library")["book"]
            self.assertTrue(b["sample"])
            self.assertEqual((b["title"], b["pages"], b["sample_preview_page"], b["progress"]),
                             ("傲慢与偏见", 321, 168, 0))
            self.assertEqual(self.browser.get("/sample.pdf").content, storage.SHOWCASE.read_bytes())
            self.assertEqual(
                self.browser.delete("/api/books/" + b["id"]).status_code, 403
            )
            self.assertEqual(
                self.browser.post(
                    "/api/account/register",
                    json={"username": "test", "password": PASSWORD},
                ).status_code,
                403,
            )
            context = usage.reader.set("demo-test")
            try:
                usage.charge()
                with self.assertRaises(HTTPException) as exc:
                    usage.charge()
                self.assertEqual(exc.exception.status_code, 429)
            finally:
                usage.reader.reset(context)
            usage.charge()
            with self.assertRaises(HTTPException):
                usage.charge()
