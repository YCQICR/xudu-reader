"""Run against a local server via BASE_URL, or the app in process. Uses real DeepSeek calls."""

import os
import time
from pathlib import Path
from fastapi.testclient import TestClient
import httpx
from backend.app import app
from backend.storage import SAMPLE

Q = "石猴跳进瀑布后看见了什么？"


def check(c):
    def data(response):
        assert response.is_success, (response.status_code, response.text[:240])
        return response.json()

    library = data(c.get("/api/library"))
    with Path(SAMPLE).open("rb") as f:
        b = data(
            c.post("/api/books", files={"file": ("西游记.pdf", f, "application/pdf")})
        )
    assert library["book"]["id"] == b["id"] and b["pages"] == 717
    path = "/api/books/" + b["id"]
    rows = data(c.get(path + "/pages/11"))["rows"]
    before = next(r["pos"] for r in rows if "我进去，我进去" in r["text"])
    after = next(r["pos"] for r in rows if "原来是座铁板桥" in r["text"])
    thread = path + "/threads/" + b["threads"][0]["id"]

    def mark(pos):
        nonlocal b
        b = data(c.put(path + "/progress", json={"pos": pos, "rev": b["rev"]}))

    def ask(question=Q):
        turn = data(
            c.post(thread + "/ask", json={"question": question, "rev": b["rev"]})
        )
        print(
            turn["answer"]["status"],
            [v["text"] for v in turn["answer"]["claims"]],
            flush=True,
        )
        return turn

    def decide(turn, action):
        return data(
            c.post(
                path + "/turns/" + turn["id"] + "/choice",
                json={"action": action, "rev": b["rev"]},
            )
        )

    mark(before)
    first = ask()
    assert first["pending"] and "铁板桥" not in str(first["answer"])
    decide(first, "decline")
    second = ask()
    spoiler = decide(second, "allow")
    print("confirmed", [v["text"] for v in spoiler["claims"]], flush=True)
    assert "桥" in str(spoiler["claims"])
    snapshot = data(c.get("/api/library"))["book"]
    assert snapshot["progress"] == before and snapshot["rev"] == b["rev"]
    track = path + "/turns/" + second["id"] + "/track"
    data(c.post(track, json={"rev": b["rev"]}))

    def card():
        return data(c.get(path + "/cards", params={"rev": b["rev"]}))[0]

    assert "铁板桥" not in str(card())
    mark(after)
    direct = ask()
    assert not direct["pending"] and "铁板桥" in str(direct["answer"]["claims"])
    assert all(
        ref["pos"] <= after for cl in direct["answer"]["claims"] for ref in cl["refs"]
    )
    direct_ref = direct["answer"]["claims"][0]["refs"][0]
    image = c.get(
        path + f"/turns/{direct['id']}/citation/{direct_ref['pos']}",
        params={"rev": b["rev"]},
    )
    assert image.is_success and image.headers["content-type"] == "image/png"
    for _ in range(90):
        current_card = card()
        if current_card["state"] != "updating":
            break
        time.sleep(1)
    assert current_card["state"] == "ready" and "铁板桥" in str(
        current_card["versions"][-1]["claims"]
    ), current_card
    assert len(current_card["versions"]) == 2, current_card
    card_ref = next(
        ref for cl in current_card["versions"][-1]["claims"]
        for ref in cl["refs"] if "铁板桥" in ref["text"]
    )
    read_page = data(c.get(path + f"/pages/{card_ref['page']}?read_only=true"))["rows"]
    assert any(
        row["pos"] == card_ref["pos"] and row["text"] == card_ref["text"]
        for row in read_page
    )
    assert all(row["pos"] <= after for row in read_page)
    mark(before)
    assert "铁板桥" not in str(card()) and card()["checked"] == before
    history = data(c.get(thread))
    assert history[-1]["hidden"] and "铁板桥" not in str(history)
    assert c.get(
        path + f"/turns/{direct['id']}/citation/{direct_ref['pos']}",
        params={"rev": b["rev"]},
    ).status_code == 404
    assert ask()["pending"]
    assert ask("帮我预报明天的天气")["answer"]["status"] == "off_topic"
    print(
        "PASS: upload, decline, one-time consent, card evidence, rollback, history and off-topic",
        flush=True,
    )


if __name__ == "__main__":
    url = os.environ.get("BASE_URL")
    with httpx.Client(base_url=url, timeout=180) if url else TestClient(app) as client:
        check(client)
