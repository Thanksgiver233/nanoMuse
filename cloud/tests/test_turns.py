"""0.31: one question, many model calls — the record and the training view count turns
beside calls (provenance.turn_start, turn_key), the console reads a turn laid out as its
steps (turnview, GET /v1/admin/record/turns), and the rows kept before the relay told the
two apart get their keys from the backfill."""

from __future__ import annotations

import json

from test_cloud import sign_up
from test_record import ADMIN, PNG_1PX, PNG_SHA, PNG_URL, relay, settled

from nanomuse_cloud import provenance, turnview
from nanomuse_cloud.db import KEY_ROWS_PER_TX, Database
from nanomuse_cloud.record import BLOB_PREFIX, pack

# -- the vocabulary, without a relay ---------------------------------------------------------


def test_the_last_message_says_whether_a_call_opened_a_turn():
    person = [{"role": "system", "content": "SOUL"}, {"role": "user", "content": "open settings"}]
    after_tool = person + [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "tap", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "tapped"},
    ]
    prefill = person + [{"role": "assistant", "content": "Sure,"}]
    assert provenance.turn_start(person) is True
    assert provenance.turn_start(after_tool) is False
    assert provenance.turn_start(prefill) is False
    # the app's step settles it when said: 1 opens, anything after continues
    assert provenance.turn_start(after_tool, step=1) is True
    assert provenance.turn_start(person, step=3) is False
    # a system prompt alone, or no messages at all, is a turn of its own
    assert provenance.turn_start([{"role": "system", "content": "x"}]) is True
    assert provenance.turn_start(None) is True
    assert [provenance.call_bucket(n) for n in (1, 2, 3, 4, 7, 8, 15, 16, 40)] == [
        "1",
        "2-3",
        "2-3",
        "4-7",
        "4-7",
        "8-15",
        "8-15",
        "16+",
        "16+",
    ]


def _call(i: int, messages: list, reply: dict, **more) -> dict:
    return {
        "id": f"c{i}",
        "ts": 1000 + i,
        "model": "m",
        "step": i,
        "status": "ok",
        "source": "cloud",
        "request": {"messages": messages},
        "response": {"message": reply},
        **more,
    }


def test_a_turn_is_laid_out_once_from_its_calls():
    shot = BLOB_PREFIX + "a" * 64
    ask = [
        {"role": "system", "content": "SOUL"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is on my screen"},
                {"type": "image_url", "image_url": {"url": BLOB_PREFIX + "b" * 64}},
            ],
        },
    ]
    tap = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "tap", "arguments": '{"x":1}'}}],
    }
    after_tap = ask + [
        tap,
        {"role": "tool", "tool_call_id": "t1", "content": "tapped"},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": shot}}]},
    ]
    read = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "t2", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
    }
    after_read = after_tap + [read, {"role": "tool", "tool_call_id": "t2", "content": "x" * 5000}]
    done = {"role": "assistant", "content": "The inbox, with two unread mails."}
    laid = turnview.lay_out([_call(1, ask, tap), _call(2, after_tap, read), _call(3, after_read, done)])
    assert laid["person"]["role"] == "user" and laid["person"]["content"][0]["text"] == "what is on my screen"
    assert laid["reply"] == "The inbox, with two unread mails." and laid["tools"] == ["tap", "read"]
    assert laid["media"] == 2  # the person's picture and the screenshot after the tap
    steps = laid["steps"]
    assert [s["call"]["id"] for s in steps] == ["c1", "c2", "c3"] and [s["messages"] for s in steps] == [2, 5, 7]
    # what came back between the calls: the tool's result and the screenshot, then the long read, cut
    assert [m["role"] for m in steps[0]["then"]] == ["tool", "user"] and steps[0]["then"][1]["content"][0]["image_url"]["url"] == shot
    assert steps[1]["then"][0]["truncated"] is True and len(steps[1]["then"][0]["content"]) == turnview.TEXT_MAX
    assert steps[2]["then"] == [] and steps[2]["reply"]["content"] == done["content"]
    # a history the app compacted between two calls: what follows the last assistant message
    compacted = [
        {"role": "system", "content": "summary so far"},
        {"role": "assistant", "content": "", "tool_calls": tap["tool_calls"]},
        {"role": "tool", "tool_call_id": "t1", "content": "tapped"},
    ]
    assert [m["role"] for m in turnview.new_since(after_tap, compacted)] == ["tool"]
    assert turnview.lay_out([]) == {"person": None, "steps": [], "reply": "", "tools": [], "media": 0}


# -- through the relay -----------------------------------------------------------------------


def _own(i: str, conversation: str, turn: int, step: int, messages: list, reply: dict, **more) -> dict:
    return {
        "id": i,
        "ts": 1792800000 + turn * 100 + step,
        "model": "gpt-5.6",
        "provider": "openai",
        "conversation": conversation,
        "run": "person",
        "turn": turn,
        "step": step,
        "messages": messages,
        "response": {"message": reply, "usage": {"prompt_tokens": 10 * step, "completion_tokens": 5}},
        **more,
    }


async def test_the_record_is_read_one_question_at_a_time(tmp_path):
    app, client, sender, up, cloud = relay(tmp_path)
    data = await sign_up(client, sender, identifier="turns-a@example.com")
    headers = {"Authorization": f"Bearer {data['api_key']}", "User-Agent": "nanoMuse-Desktop/1.0.7", "X-NanoMuse-Device": "mac-1"}
    assert (await client.post("/v1/me/contribute", headers=headers, json={"on": True})).status_code == 200
    r = await client.put(f"/v1/blobs/{PNG_SHA}", headers={**headers, "Content-Type": "image/png"}, content=PNG_1PX)
    assert r.status_code == 200
    # one question, three calls: look (a screenshot comes back), tap, answer
    ask = [{"role": "system", "content": "SOUL"}, {"role": "user", "content": "open the inbox"}]
    look = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "computer_look", "arguments": "{}"}}],
    }
    seen = ask + [
        look,
        {"role": "tool", "tool_call_id": "t1", "content": "screen"},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": BLOB_PREFIX + PNG_SHA}}]},
    ]
    tap = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "t2", "type": "function", "function": {"name": "computer_tap", "arguments": '{"x":3,"y":4}'}}],
    }
    tapped = seen + [tap, {"role": "tool", "tool_call_id": "t2", "content": "ok"}]
    done = {"role": "assistant", "content": "The inbox is open."}
    for body in (
        _own("q1-1", "conv-1", 1, 1, ask, look, trajectory="traj-1", trajectory_step=1),
        _own("q1-2", "conv-1", 1, 2, seen, tap, trajectory="traj-1", trajectory_step=2),
        _own("q1-3", "conv-1", 1, 3, tapped, done, trajectory="traj-1", trajectory_step=3),
    ):
        assert (await client.post("/v1/turns", headers=headers, json=body)).status_code == 201
    # a second question in the same chat, answered in one call, and one in another chat that
    # said nothing of turns and steps (its last message settles it)
    second = tapped + [done, {"role": "user", "content": "thanks, now archive it"}]
    assert (
        await client.post(
            "/v1/turns", headers=headers, json=_own("q2-1", "conv-1", 2, 1, second, {"role": "assistant", "content": "Archived."})
        )
    ).status_code == 201
    quiet = {**_own("q3-1", "conv-2", 3, 1, [{"role": "user", "content": "hello"}], {"role": "assistant", "content": "hi"})}
    quiet.pop("turn"), quiet.pop("step")
    assert (await client.post("/v1/turns", headers=headers, json=quiet)).status_code == 201
    await settled(app)
    own = f"own-{data['account']['id'][:8]}-"

    # the rows: the opener keys itself, the calls after it take its key
    rows = cloud.db._conn.execute("SELECT id, turn_start, turn_key FROM turns ORDER BY ts").fetchall()
    assert [(r["id"][-4:], r["turn_start"], r["turn_key"][-4:]) for r in rows] == [
        ("q1-1", 1, "q1-1"),
        ("q1-2", 0, "q1-1"),
        ("q1-3", 0, "q1-1"),
        ("q2-1", 1, "q2-1"),
        ("q3-1", 1, "q3-1"),
    ]
    # the training view's shape rows say the same, keyed by the record's ids
    shapes = cloud.db._conn.execute("SELECT turn_id, turn_start, turn_key, conversation, turn_no FROM sample_stats ORDER BY ts").fetchall()
    assert [(r["turn_id"][-4:], r["turn_start"], r["turn_key"][-4:], r["conversation"], r["turn_no"]) for r in shapes] == [
        ("q1-1", 1, "q1-1", "conv-1", 1),
        ("q1-2", 0, "q1-1", "conv-1", 1),
        ("q1-3", 0, "q1-1", "conv-1", 1),
        ("q2-1", 1, "q2-1", "conv-1", 2),
        ("q3-1", 1, "q3-1", "conv-2", 0),
    ]

    # the figures count both ways
    view = (await client.get("/v1/admin/record?days=3650", headers=ADMIN)).json()
    assert view["totals"]["n"] == 5 and view["totals"]["questions"] == 3 and view["period"]["questions"] == 3
    q = view["questions"]
    # with_media counts the questions whose calls carried a picture, the history included: the
    # second question's call repeated the first's screenshot
    assert q["n"] == 3 and q["pending"] == 0 and q["with_media"] == 2 and q["with_hands"] == 1
    assert q["calls_per_question"] == {"1": 2, "2-3": 1, "4-7": 0, "8-15": 0, "16+": 0} and abs(q["calls_mean"] - 5 / 3) < 1e-9
    dv = (await client.get("/v1/admin/data?days=3650", headers=ADMIN)).json()
    assert dv["totals"]["samples"] == 5 and dv["totals"]["turns"] == 3 and dv["by_source"] == [{"source": "own", "samples": 5, "turns": 3}]
    assert dv["by_account"][0]["samples"] == 5 and dv["by_account"][0]["turns"] == 3

    # the console's view: newest question first, each laid out as its steps
    got = (await client.get("/v1/admin/record/turns", headers=ADMIN)).json()
    assert got["total"] == 3 and got["pending"] == 0 and [t["key"][-4:] for t in got["turns"]] == ["q3-1", "q2-1", "q1-1"]
    t = got["turns"][2]
    assert t["calls"] == 3 and t["ts0"] == 1792800101 and t["ts1"] == 1792800103 and t["hint"] == data["account"]["hint"]
    assert t["conversation"] == "conv-1" and t["run"] == "person" and t["turn"] == 1 and t["trajectory"] == "traj-1"
    assert t["platform"] == "desktop" and t["source"] == "own" and t["models"] == ["gpt-5.6"] and t["media"] == 1
    assert t["prompt_tokens"] == 60 and t["completion_tokens"] == 15 and t["tools"] == ["computer_look", "computer_tap"]
    assert t["person"] == {"role": "user", "content": "open the inbox"} and t["reply"] == "The inbox is open."
    steps = t["steps"]
    assert [s["call"]["id"][-4:] for s in steps] == ["q1-1", "q1-2", "q1-3"] and [s["call"]["trajectory_step"] for s in steps] == [1, 2, 3]
    assert steps[0]["reply"]["tool_calls"][0]["function"]["name"] == "computer_look"
    assert [m["role"] for m in steps[0]["then"]] == ["tool", "user"] and steps[0]["then"][1]["content"][0]["image_url"][
        "url"
    ] == BLOB_PREFIX + PNG_SHA
    assert steps[1]["then"] == [{"role": "tool", "tool_call_id": "t2", "content": "ok"}] and steps[2]["then"] == []
    # the second question's view carries only what is new: not the first question again
    t2 = got["turns"][1]
    assert t2["calls"] == 1 and t2["person"]["content"] == "thanks, now archive it" and t2["steps"][0]["messages"] == len(second)
    # the picture is fetched by its hash, as the console does
    assert (await client.get(f"/v1/admin/blobs/{PNG_SHA}", headers=ADMIN)).content == PNG_1PX

    # filters: the hands, pictures, a conversation, a run word nobody said; and paging
    assert [t["key"][-4:] for t in (await client.get("/v1/admin/record/turns?hands=1", headers=ADMIN)).json()["turns"]] == ["q1-1"]
    assert [t["key"][-4:] for t in (await client.get("/v1/admin/record/turns?media=1", headers=ADMIN)).json()["turns"]] == ["q2-1", "q1-1"]
    assert t2["media"] == 0  # the laid-out view counts only what the question itself brought
    assert [t["key"][-4:] for t in (await client.get("/v1/admin/record/turns?conversation=conv-2", headers=ADMIN)).json()["turns"]] == [
        "q3-1"
    ]
    assert (await client.get("/v1/admin/record/turns?run=schedule", headers=ADMIN)).json()["turns"] == []
    assert (
        await client.get(f"/v1/admin/record/turns?account_id={data['account']['id']}&platform=desktop&source=own", headers=ADMIN)
    ).json()["total"] == 3
    page = (await client.get("/v1/admin/record/turns?limit=1", headers=ADMIN)).json()["turns"]
    assert [t["key"][-4:] for t in page] == ["q3-1"]
    page = (await client.get(f"/v1/admin/record/turns?limit=2&before={page[0]['ts1']}&before_key={page[0]['key']}", headers=ADMIN)).json()[
        "turns"
    ]
    assert [t["key"][-4:] for t in page] == ["q2-1", "q1-1"]

    # one call in full, system prompt and all
    one = (await client.get(f"/v1/admin/turns/{own}q1-2", headers=ADMIN)).json()["turn"]
    assert (
        one["request"]["messages"][0] == {"role": "system", "content": "SOUL"}
        and one["turn_key"] == f"{own}q1-1"
        and one["turn_start"] is False
    )
    assert one["hint"] == data["account"]["hint"]
    assert (await client.get("/v1/admin/turns/nothing-here", headers=ADMIN)).status_code == 404
    # the exports carry the key, so a set built from them groups a question's calls
    lines = [json.loads(ln) for ln in (await client.get("/v1/admin/turns/export", headers=ADMIN)).text.splitlines() if ln]
    assert {ln["turn_key"][-4:] for ln in lines} == {"q1-1", "q2-1", "q3-1"} and sum(1 for ln in lines if ln["turn_start"]) == 3


async def test_the_rows_kept_before_get_their_keys_from_the_backfill(tmp_path):
    app, client, sender, up, cloud = relay(tmp_path)
    data = await sign_up(client, sender, identifier="turns-b@example.com")
    headers = {"Authorization": f"Bearer {data['api_key']}", "User-Agent": "nanoMuse-Android/1.0.8", "X-NanoMuse-Conversation": "c-1"}
    assert (await client.post("/v1/me/contribute", headers=headers, json={"on": True})).status_code == 200
    ask = [{"role": "user", "content": "find the file"}]
    read = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "r1", "type": "function", "function": {"name": "read", "arguments": "{}"}}],
    }
    after = ask + [read, {"role": "tool", "tool_call_id": "r1", "content": "found"}]
    for messages in (ask, after, [{"role": "user", "content": "and the next one"}]):
        assert (
            await client.post("/v1/chat/completions", headers=headers, json={"model": "qwen3.8-27b", "messages": messages})
        ).status_code == 200
    # one pictured turn, so the keyed figures see it
    pictured = [{"role": "user", "content": [{"type": "text", "text": "this"}, {"type": "image_url", "image_url": {"url": PNG_URL}}]}]
    assert (
        await client.post(
            "/v1/chat/completions",
            headers={**headers, "X-NanoMuse-Conversation": "c-2"},
            json={"model": "qwen3.8-27b", "messages": pictured},
        )
    ).status_code == 200
    await settled(app)
    # the relay before 0.31 left these columns unset: undo what the writes just did
    with cloud.db.tx() as c:
        c.execute("UPDATE turns SET turn_start=-1, turn_key=''")
        c.execute("UPDATE sample_stats SET turn_start=-1, turn_key='', conversation='', turn_no=0")
    view = (await client.get("/v1/admin/record?days=3", headers=ADMIN)).json()
    assert view["questions"] == {
        "n": 0,
        "pending": 4,
        "with_media": 0,
        "with_hands": 0,
        "calls_per_question": {"1": 0, "2-3": 0, "4-7": 0, "8-15": 0, "16+": 0},
        "calls_mean": 0.0,
    }
    assert (await client.get("/v1/admin/data?days=3", headers=ADMIN)).json()["totals"] == {
        "samples": 4,
        "turns": 0,
        "turns_pending": 4,
        "prompt_tokens": 400,
        "completion_tokens": 200,
    }
    assert (await client.get("/v1/admin/record/turns", headers=ADMIN)).json() == {"turns": [], "pending": 4, "total": 0, "places": {}}

    analytics = app.state.analytics
    analytics.keys_done = False
    read_rows = 0
    for _ in range(10):
        n = analytics.turn_keys_step(batch=2)
        read_rows += n
        if analytics.keys_done:
            break
    assert analytics.keys_done and read_rows == 8  # four rows of each table, two a page
    assert cloud.db.settings_get("turns.keys_cursor")[0] == [-1, -1] and cloud.db.settings_get("sample_stats.keys_cursor")[0] == [-1, -1]
    rows = cloud.db._conn.execute("SELECT turn_start, turn_key, conversation FROM turns ORDER BY ts, rowid").fetchall()
    keys = [r["turn_key"] for r in rows]
    assert [r["turn_start"] for r in rows] == [1, 0, 1, 1] and keys[1] == keys[0] and len({keys[0], keys[2], keys[3]}) == 3
    shapes = cloud.db._conn.execute("SELECT turn_start, turn_key, conversation FROM sample_stats ORDER BY ts, rowid").fetchall()
    assert [(r["turn_start"], r["conversation"]) for r in shapes] == [(1, "c-1"), (0, "c-1"), (1, "c-1"), (1, "c-2")]
    assert [r["turn_key"] for r in shapes] == keys  # the training view's keys are the record's ids
    view = (await client.get("/v1/admin/record?days=3", headers=ADMIN)).json()
    assert view["questions"]["n"] == 3 and view["questions"]["pending"] == 0 and view["questions"]["with_media"] == 1
    assert view["questions"]["calls_per_question"]["1"] == 2 and view["questions"]["calls_per_question"]["2-3"] == 1
    got = (await client.get("/v1/admin/record/turns", headers=ADMIN)).json()
    assert got["pending"] == 0 and sorted(t["calls"] for t in got["turns"]) == [1, 1, 2]  # all within one second: no order to assert
    two = next(t for t in got["turns"] if t["calls"] == 2)
    assert two["steps"][0]["then"] == [{"role": "tool", "tool_call_id": "r1", "content": "found"}] and two["reply"] == "hi"
    # once through, the step is a no-op
    assert analytics.turn_keys_step() == 0


# -- the figures never read the bodies; the backfill holds the lock for a moment --------------


def _turn(db: Database, i: int, start: bool, keyed: bool = True) -> None:
    """One kept call of a conversation: the person's message opens a turn, a tool's result
    continues one. ``keyed`` as 0.31 writes it; else as a relay before left it (no key)."""
    last = {"role": "user", "content": f"question {i}"} if start else {"role": "tool", "tool_call_id": f"r{i}", "content": "found"}
    row = {
        "id": f"t-{i:04d}",
        "account_id": "acc-1",
        "ts": 1_700_000_000 + i,
        "conversation": "c-1",
        "request": pack({"messages": [{"role": "system", "content": "SOUL"}, last]}),
        "response": pack({"message": {"role": "assistant", "content": "ok"}}),
        "prompt_tokens": 10,
        "completion_tokens": 5,
    }
    if keyed:
        row["turn_start"] = start
    db.add_turn(row, [])


def test_the_record_figures_read_their_index_and_never_the_rows(tmp_path):
    """A turn's row carries its request, and the columns the figures sum come after it in
    the row: a sum that reads them walks every body. The figures and the question-by-
    question view read `turns_figures` instead — whatever the planner's statistics say,
    since the query names the index."""
    db = Database(str(tmp_path / "relay.db"))
    for i in range(6):
        _turn(db, i, start=i % 3 == 0)
    seen: list[str] = []
    db._conn.set_trace_callback(seen.append)
    since = 1_700_000_000
    db.turns_by_day(since, 0)
    db.record_totals(0)
    db.record_totals(since, "acc-1")
    db.turn_figures(since)
    db.turn_figures(since, "acc-1")
    db.calls_per_turn(since)
    db.calls_per_turn(since, "acc-1")
    db.turn_groups(since)
    db.turn_groups(since, account_id="acc-1", conversation="c-1")
    db.turn_groups(since, media=True, hands=True, script="han", platform="android", run="person", model="m", source="cloud")
    db.turn_groups(since, before=since + 5, before_key="t-0003")
    db._conn.set_trace_callback(None)
    plans = {sql: " | ".join(r[3] for r in db._conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()) for sql in seen if "FROM turns" in sql}
    assert len(plans) == 11
    for sql, plan in plans.items():
        assert "USING COVERING INDEX turns_figures" in plan and "SEARCH turns USING INDEX" not in plan, (sql, plan)
    # and the figures are right
    assert db.turn_figures(since)["turns"] == 2 and db.calls_per_turn(since) == [(3, 2)]
    # the page's calls and their events come by the partial indexes: a look-up by key alone
    # did not reach them (their WHERE must be repeated) and scanned the whole record instead
    seen.clear()
    db._conn.set_trace_callback(seen.append)
    assert [r["id"] for r in db.turns_of(["t-0000", "t-0003"])] == ["t-0000", "t-0001", "t-0002", "t-0003", "t-0004", "t-0005"]
    db.client_events_for_turns(["t-0000"])
    db._conn.set_trace_callback(None)
    plans = [" | ".join(r[3] for r in db._conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()) for sql in seen if sql.startswith("SELECT")]
    assert (
        len(plans) == 2 and "USING INDEX turns_key (turn_key=?)" in plans[0] and "USING INDEX client_events_turn (turn_id=?)" in plans[1]
    ), plans


def test_a_page_of_keys_is_written_a_few_rows_at_a_time(tmp_path):
    """The backfill's page goes in transactions of KEY_ROWS_PER_TX (an update rewrites the
    row, body and all), and a row that continues a turn finds its opener's key across
    the transactions before it."""
    db = Database(str(tmp_path / "relay.db"))
    n = 2 * KEY_ROWS_PER_TX + 7
    for i in range(n):
        _turn(db, i, start=i == 0, keyed=False)
    commits: list[str] = []
    db._conn.set_trace_callback(lambda sql: commits.append(sql) if sql.upper().startswith("COMMIT") else None)
    rows = db.turns_unkeyed_page(0, 0, 1000)
    assert len(rows) == n
    written = db.set_turn_keys(
        "turns",
        [
            {
                "rowid": r["rowid"],
                "id": r["id"],
                "account_id": r["account_id"],
                "ts": r["ts"],
                "conversation": r["conversation"],
                "turn_no": 0,
                "start": i == 0,
            }
            for i, r in enumerate(rows)
        ],
    )
    db._conn.set_trace_callback(None)
    assert written == n and len(commits) == 3  # 25 + 25 + 7
    keyed = db._conn.execute("SELECT turn_start, turn_key FROM turns ORDER BY ts").fetchall()
    assert [r["turn_start"] for r in keyed] == [1] + [0] * (n - 1) and {r["turn_key"] for r in keyed} == {"t-0000"}
