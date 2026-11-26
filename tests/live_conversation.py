"""Exercise actual multi-turn answers and references on both sample books."""

import os
import tempfile
import time

from fastapi.testclient import TestClient


def run(client, book, pos, turns):
    path = "/api/books/" + book["id"]
    thread = path + "/threads/" + book["threads"][0]["id"]
    marked = client.put(path + "/progress", json={"rev": book["rev"], "pos": pos})
    marked.raise_for_status()
    rev = marked.json()["rev"]
    for question, expected in turns:
        started = time.monotonic()
        reply = client.post(thread + "/ask", json={"rev": rev, "question": question})
        if not reply.is_success:
            raise AssertionError(reply.json())
        turn = reply.json()
        answer = turn["answer"]
        body = " ".join(cl["text"] for cl in answer["claims"])
        refs = [ref for cl in answer["claims"] for ref in cl["refs"]]
        print(question, f"{time.monotonic() - started:.1f}s", body, flush=True)
        assert answer["status"] == "answer" and any(word in body for word in expected)
        assert refs and all(0 < ref["pos"] <= pos for ref in refs)
        image = client.get(
            path + f"/turns/{turn['id']}/citation/{refs[0]['pos']}", params={"rev": rev}
        )
        assert image.is_success and image.headers["content-type"].startswith("image/")
        client.get(thread).raise_for_status()
    assert len(client.get(thread).json()) == len(turns)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temporary:
        os.environ["XUDU_DATA"] = temporary
        from backend.app import app
        from backend.storage import ROOT

        with TestClient(app) as client:
            lib = client.get("/api/library")
            lib.raise_for_status()
            run(
                client,
                lib.json()["book"],
                148,
                [
                    ("石猴为什么觉得大家应该拜他为王？", ("约定", "说好", "先前")),
                    (
                        "那他是怎么说服大家的？用现在的话讲给我听。",
                        ("约定", "算数", "说过", "答应"),
                    ),
                ],
            )
            second = ROOT / "samples" / "emma.pdf"
            with second.open("rb") as pdf:
                response = client.post(
                    "/api/books", files={"file": ("爱玛.pdf", pdf, "application/pdf")}
                )
            response.raise_for_status()
            run(
                client,
                response.json(),
                11,
                [
                    ("Miss Taylor 在 Woodhouse 家待了多少年？", ("十六", "16")),
                    ("那她后来发生了什么变化？", ("结婚", "嫁")),
                ],
            )
