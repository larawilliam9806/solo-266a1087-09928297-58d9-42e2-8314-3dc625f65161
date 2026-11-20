import html
import io
import json
import os
import re
import sqlite3
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SURVEY_DB", str(tmp_path / "survey.sqlite3"))
    monkeypatch.setenv("SURVEY_SECRET", "test")
    for module in ("storage", "engine", "importer", "main"):
        sys.modules.pop(module, None)
    import main
    main.app.config.update(TESTING=True)
    with main.app.test_client() as test_client:
        yield test_client


CSV_TEXT = """户号,Q1 设备来源,Q2 自购设备类型,Q3 自购台数,Q4 是否享受购置补贴,Q5 租赁方式,Q6 租赁台数,Q7 年费用合计（元）
H1,自购,喷药机,2,无,,,12000
H2,租赁,喷药机,2,有,按年整租,3,8000
H3,自购,,,,,,9000
H4,租赁,,,有,按季短租,,,
"""

JUMPS = {
    "order": ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7"],
    "titles": {
        "Q1": "设备来源", "Q2": "自购设备类型", "Q3": "自购台数",
        "Q4": "是否享受购置补贴", "Q5": "租赁方式", "Q6": "租赁台数",
        "Q7": "年费用合计（元）",
    },
    "types": {"Q1": "single", "Q2": "single", "Q4": "single",
              "Q5": "single", "Q3": "number", "Q6": "number",
              "Q7": "number"},
    "options": {"Q1": ["自购", "租赁"], "Q2": ["喷药机", "采摘平台"],
                "Q4": ["有", "无"], "Q5": ["按年整租", "按季短租"]},
    "jumps": {"Q1": {"自购": "Q2", "租赁": "Q5"},
              "Q2": {"采摘平台": "Q7"},
              "Q4": {"有": "Q7", "无": "Q7"}},
}


def upload(client, csv_text=CSV_TEXT, jumps=JUMPS):
    response = client.post("/upload", data={
        "csv_file": (io.BytesIO(csv_text.encode("utf-8")), "answers.csv"),
        "json_file": (io.BytesIO(json.dumps(jumps).encode("utf-8")),
                      "jumps.json"),
        "batch_name": "测试批次",
    }, content_type="multipart/form-data", follow_redirects=True)
    assert response.status_code == 200
    return response


def form_fields(body):
    """``[qid, prefix, original, value]`` for every question on the page."""
    entries = []
    pattern = re.compile(
        r'name="k_qid_(\d+)"\s+value="([^"]*)">\s*'
        r'<input\s+type="hidden"\s+name="k_prefix_\1"\s+value="([^"]*)">\s*'
        r'<input\s+type="hidden"\s+name="k_orig_\1"\s+value="([^"]*)"',
        re.S)
    for index, qid, prefix, original in pattern.findall(body):
        tags = re.findall(r'<input[^>]*name="v_%s"[^>]*>' % index, body, re.S)
        chosen = next((tag for tag in tags if "checked" in tag),
                      tags[0] if tags else "")
        value_match = re.search(r'data-value="([^"]*)"', chosen)
        entries.append([html.unescape(qid),
                        tuple(json.loads(html.unescape(prefix))),
                        html.unescape(original),
                        html.unescape(value_match.group(1)) if value_match
                        else ""])
    return entries


def values(body):
    return {(qid, prefix): value
            for qid, prefix, _original, value in form_fields(body)}


def submit(client, entries, household=2):
    data = {}
    for position, (qid, prefix, original, value) in enumerate(entries, 1):
        data["k_qid_%d" % position] = qid
        data["k_prefix_%d" % position] = json.dumps(list(prefix))
        data["k_orig_%d" % position] = original
        data["v_%d" % position] = value
    return client.post("/batch/1/household/%d" % household, data=data,
                       follow_redirects=True).get_data(as_text=True)


def replace(entries, qid, prefix, value):
    return [[q, p, o, value if (q == qid and tuple(p) == tuple(prefix))
             else v] for q, p, o, v in entries]


def fill(client, body, household, edits):
    """Save edits one at a time, following the re-rendered page.

    The walk stops at the first blank question, so a freshly entered branch
    reveals one question per save.
    """
    for qid, prefix, value in edits:
        body = submit(client, replace(form_fields(body), qid, prefix, value),
                      household=household)
    return body


SELF = ("Q1", "Q2", "Q3", "Q4")
RENT = ("Q1", "Q5", "Q6")


def test_import_and_list(client):
    body = upload(client).get_data(as_text=True)
    assert "测试批次" in body
    for household in ("H1", "H2", "H3", "H4"):
        assert household in body


def test_self_buy_path_and_shared_cost(client):
    upload(client)
    fields = form_fields(
        client.get("/batch/1/household/1").get_data(as_text=True))
    assert [qid for qid, _, _, _ in fields] == ["Q1", "Q2", "Q3", "Q4", "Q7"]
    assert dict((q, v) for q, _, _, v in fields)["Q7"] == "12000"


def test_rental_path_skips_self_buy_questions(client):
    upload(client)
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    assert [qid for qid, _, _, _ in fields] == ["Q1", "Q5", "Q6", "Q7"]
    assert dict((q, v) for q, _, _, v in fields)["Q7"] == "8000"


def test_import_ignores_unreached_answers(client):
    upload(client)
    text = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    header = text.splitlines()[0].split(",")
    h2 = next(line for line in text.splitlines() if line.startswith("H2"))
    cells = h2.split(",")
    # H2 is a rental household; its self-buy columns must not be imported.
    assert cells[header.index("Q2 自购设备类型")] == ""
    assert cells[header.index("Q3 自购台数")] == ""
    assert cells[header.index("Q4 是否享受购置补贴")] == ""
    assert cells[header.index("当前路径")] == "Q1 → Q5 → Q6 → Q7"
    assert cells[header.index("Q5 租赁方式")] == "按年整租"


def test_switch_branch_saves_separate_answers(client):
    upload(client)
    # H2 starts on the rental route; switch the source question and save.
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    body = submit(client, replace(fields, "Q1", (), "自购"))

    # The page now walks the self-buy route; fill it in and save again.
    assert [qid for qid, _, _, _ in form_fields(body)] == ["Q1", "Q2"]
    body = fill(client, body, 2, [
        ("Q2", ("Q1",), "喷药机"),
        ("Q3", ("Q1", "Q2"), "4"),
        ("Q4", ("Q1", "Q2", "Q3"), "无"),
        ("Q7", SELF, "15000"),
    ])

    assert "其他路径上保存的答案" in body
    current = values(body)
    assert current[("Q1", ())] == "自购"
    assert current[("Q7", SELF)] == "15000"
    # The rental path's answers are still stored.
    assert "按年整租" in body and "8000" in body

    # Switch back to rental: its own answers return, including its cost.
    body = submit(client, replace(form_fields(body), "Q1", (), "租赁"))
    current = values(body)
    assert current[("Q5", ("Q1",))] == "按年整租"
    assert current[("Q6", ("Q1", "Q5"))] == "3"
    assert current[("Q7", RENT)] == "8000"


def test_shared_cost_question_is_per_path(client):
    upload(client)
    # Change the rental household's cost.
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    body = submit(client, replace(fields, "Q7", RENT, "9000"))
    assert values(body)[("Q7", RENT)] == "9000"

    # Move to self-buy: save the route switch, then fill the new branch.
    body = submit(client, replace(form_fields(body), "Q1", (), "自购"))
    body = fill(client, body, 2, [
        ("Q2", ("Q1",), "喷药机"),
        ("Q3", ("Q1", "Q2"), "4"),
        ("Q4", ("Q1", "Q2", "Q3"), "无"),
    ])
    current = values(body)
    assert current[("Q7", SELF)] == ""
    assert "9000" in body  # still visible as another path's answer

    # Return to rental: 9000 is intact.
    body = submit(client, replace(form_fields(body), "Q1", (), "租赁"))
    assert values(body)[("Q7", RENT)] == "9000"


def test_stale_fields_of_old_route_are_not_saved(client):
    upload(client)
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    # Keep every rental field as shown but change the route: only the
    # Q1 edit is applied, the untouched fields follow no one anywhere.
    body = submit(client, replace(fields, "Q1", (), "自购"))
    current = values(body)
    assert [q for q, _ in current] == ["Q1", "Q2"]
    assert current[("Q2", ("Q1",))] == ""


def test_two_windows_keep_each_others_edits(client):
    """The reported bug: one household open in two windows."""
    upload(client)
    page = client.get("/batch/1/household/1").get_data(as_text=True)
    window_a = form_fields(page)
    window_b = form_fields(page)

    # Window A: 自购台数 2 -> 3.
    body = submit(client, replace(window_a, "Q3", ("Q1", "Q2"), "3"),
                  household=1)
    assert values(body)[("Q3", ("Q1", "Q2"))] == "3"

    # Window B (stale page, still shows 台数=2): only edits 年费用.
    body = submit(client, replace(window_b, "Q7", SELF, "13000"),
                  household=1)
    current = values(body)
    assert current[("Q7", SELF)] == "13000"
    # 台数 must not fall back to the stale 2.
    assert current[("Q3", ("Q1", "Q2"))] == "3"

    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    header = export.splitlines()[0].split(",")
    h1 = next(line for line in export.splitlines() if line.startswith("H1"))
    cells = h1.split(",")
    assert cells[header.index("Q3 自购台数")] == "3"
    assert cells[header.index("Q7 年费用合计（元）")] == "13000"


def test_conflicting_edit_rejects_whole_save(client):
    upload(client)
    page = client.get("/batch/1/household/1").get_data(as_text=True)
    window_a = form_fields(page)
    window_b = form_fields(page)

    # Window A: 台数 2 -> 3.
    submit(client, replace(window_a, "Q3", ("Q1", "Q2"), "3"), household=1)
    # Window B also edited 台数 (2 -> 5) plus 年费用: whole save rejected.
    entries = replace(window_b, "Q3", ("Q1", "Q2"), "5")
    entries = replace(entries, "Q7", SELF, "13000")
    body = submit(client, entries, household=1)
    assert "保存失败" in body and "Q3" in body

    # Nothing from B was saved: Q3 stays 3, Q7 stays 12000.
    current = values(client.get("/batch/1/household/1").get_data(as_text=True))
    assert current[("Q3", ("Q1", "Q2"))] == "3"
    assert current[("Q7", SELF)] == "12000"
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    h1 = next(line for line in export.splitlines() if line.startswith("H1"))
    assert "13000" not in h1


def test_edit_on_unreachable_path_rejects_whole_save(client):
    upload(client)
    page = client.get("/batch/1/household/1").get_data(as_text=True)
    window_a = form_fields(page)
    window_b = form_fields(page)

    # Window A switches the household to the rental route.
    submit(client, replace(window_a, "Q1", (), "租赁"), household=1)
    # Window B edited 台数, which is no longer reachable on its old path.
    body = submit(client, replace(window_b, "Q3", ("Q1", "Q2"), "5"),
                  household=1)
    assert "保存失败" in body and "Q3" in body

    current = values(client.get("/batch/1/household/1").get_data(as_text=True))
    assert current[("Q1", ())] == "租赁"


def test_same_edit_from_two_windows_is_not_a_conflict(client):
    upload(client)
    page = client.get("/batch/1/household/1").get_data(as_text=True)
    window_a = form_fields(page)
    window_b = form_fields(page)
    body = submit(client, replace(window_a, "Q3", ("Q1", "Q2"), "3"),
                  household=1)
    assert "已保存" in body
    body = submit(client, replace(window_b, "Q3", ("Q1", "Q2"), "3"),
                  household=1)
    assert "保存失败" not in body
    assert values(body)[("Q3", ("Q1", "Q2"))] == "3"


def test_interleaved_saves_keep_both_edits(client):
    """Two windows saving at the same time, each editing one question."""
    upload(client)
    import main
    page = client.get("/batch/1/household/1").get_data(as_text=True)
    entries = {
        "a": replace(form_fields(page), "Q3", ("Q1", "Q2"), "3"),
        "b": replace(form_fields(page), "Q7", SELF, "13000"),
    }
    barrier = threading.Barrier(2)
    bodies = {}

    def save(name):
        with main.app.test_client() as own_client:
            barrier.wait(timeout=10)
            bodies[name] = submit(own_client, entries[name], household=1)

    threads = [threading.Thread(target=save, args=(name,)) for name in "ab"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)

    assert sum("已保存 1 处修改" in body for body in bodies.values()) == 2
    current = values(client.get("/batch/1/household/1").get_data(as_text=True))
    assert current[("Q3", ("Q1", "Q2"))] == "3"
    assert current[("Q7", SELF)] == "13000"


def test_interleaved_conflicting_saves_exactly_one_wins(client):
    """Interleaved saves of the same question follow the same rules."""
    upload(client)
    import main
    page = client.get("/batch/1/household/1").get_data(as_text=True)
    entries = {
        "a": replace(form_fields(page), "Q3", ("Q1", "Q2"), "3"),
        "b": replace(form_fields(page), "Q3", ("Q1", "Q2"), "5"),
    }
    barrier = threading.Barrier(2)
    bodies = {}

    def save(name):
        with main.app.test_client() as own_client:
            barrier.wait(timeout=10)
            bodies[name] = submit(own_client, entries[name], household=1)

    threads = [threading.Thread(target=save, args=(name,)) for name in "ab"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)

    assert sum("已保存" in body for body in bodies.values()) == 1
    losers = [body for body in bodies.values() if "保存失败" in body]
    assert len(losers) == 1 and "Q3" in losers[0]

    current = values(client.get("/batch/1/household/1").get_data(as_text=True))
    assert current[("Q3", ("Q1", "Q2"))] in ("3", "5")
    # The CSV download agrees with the page.
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    header = export.splitlines()[0].split(",")
    h1 = next(line for line in export.splitlines() if line.startswith("H1"))
    assert h1.split(",")[header.index("Q3 自购台数")] == \
        current[("Q3", ("Q1", "Q2"))]


def test_invalid_single_option_rejected_and_previous_answers_kept(client):
    upload(client)
    response = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "k_orig_1": "租赁", "v_1": "丢弃",
    }, follow_redirects=True)
    assert "保存失败" in response.get_data(as_text=True)
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    assert "按年整租" in export and "8000" in export


def test_export_lists_effective_answers_and_first_missing(client):
    upload(client)
    text = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    lines = text.strip().splitlines()
    header = lines[0].split(",")
    assert header[0] == "户号"
    assert "当前路径" in header
    assert "Q7 年费用合计（元）" in header
    assert header[-2] == "首个漏答题"

    h2 = next(line for line in lines if line.startswith("H2"))
    cells = h2.split(",")
    assert cells[header.index("首个漏答题")] == ""
    assert cells[header.index("Q1 设备来源")] == "租赁"
    assert cells[header.index("Q5 租赁方式")] == "按年整租"
    assert cells[header.index("Q7 年费用合计（元）")] == "8000"


def test_missing_answer_is_reported(client):
    upload(client)
    text = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    header = text.splitlines()[0].split(",")
    h3 = next(line for line in text.splitlines() if line.startswith("H3"))
    cells = h3.split(",")
    assert cells[header.index("Q1 设备来源")] == "自购"
    assert cells[header.index("首个漏答题")] == "Q2"
    assert cells[header.index("漏答题干")] == "自购设备类型"
    # Nothing after the first blank is imported, even though Q7 was filled.
    assert cells[header.index("Q7 年费用合计（元）")] == ""

    h4 = next(line for line in text.splitlines() if line.startswith("H4"))
    cells = h4.split(",")
    assert cells[header.index("Q5 租赁方式")] == "按季短租"
    assert cells[header.index("首个漏答题")] == "Q6"


def test_clearing_answer_stops_path(client):
    upload(client)
    fields = form_fields(
        client.get("/batch/1/household/2").get_data(as_text=True))
    body = submit(client, replace(fields, "Q5", ("Q1",), ""))
    assert [qid for qid, _, _, _ in form_fields(body)] == ["Q1", "Q5"]


def test_jump_directly_to_shared_cost_question(client):
    upload(client)
    # 自购 + 采摘平台 jumps straight to Q7, skipping Q3/Q4.
    body = client.post("/batch/1/household/1", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "k_orig_1": "自购", "v_1": "自购",
        "k_qid_2": "Q2", "k_prefix_2": '["Q1"]', "k_orig_2": "喷药机",
        "v_2": "采摘平台",
    }, follow_redirects=True).get_data(as_text=True)
    current = values(body)
    assert [q for q, _ in current] == ["Q1", "Q2", "Q7"]
    assert current[("Q7", ("Q1", "Q2"))] == ""


def test_save_failure_keeps_last_saved_answers(client, monkeypatch):
    upload(client)
    import main
    original = main.storage.save_answers
    monkeypatch.setattr(main.storage, "save_answers",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            RuntimeError("disk full")))
    response = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "k_orig_1": "租赁", "v_1": "自购",
    }, follow_redirects=True)
    assert "disk full" in response.get_data(as_text=True)

    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    h2 = next(line for line in export.splitlines() if line.startswith("H2"))
    assert "租赁" in h2 and "8000" in h2
    page = client.get("/batch/1/household/2").get_data(as_text=True)
    assert values(page)[("Q1", ())] == "租赁"
    assert values(page)[("Q7", RENT)] == "8000"
    monkeypatch.setattr(main.storage, "save_answers", original)


def test_database_error_keeps_download_on_last_saved(client, monkeypatch):
    upload(client)
    import main

    def explode(connection, household_id, changes):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(main.storage, "save_answers", explode)
    response = client.post("/batch/1/household/2", data={
        "k_qid_1": "Q1", "k_prefix_1": "[]", "k_orig_1": "租赁", "v_1": "自购",
    }, follow_redirects=True)
    assert "保存失败" in response.get_data(as_text=True)
    export = client.get("/batch/1/export.csv").data.decode("utf-8-sig")
    h2 = next(line for line in export.splitlines() if line.startswith("H2"))
    assert "租赁" in h2 and "8000" in h2


def test_batch_route_returns_404_for_missing_batch(client):
    assert client.get("/batch/99").status_code == 404
    assert client.get("/batch/1/household/99").status_code == 404


def test_import_rejects_bad_json(client):
    response = client.post("/upload", data={
        "csv_file": (io.BytesIO(CSV_TEXT.encode()), "answers.csv"),
        "json_file": (io.BytesIO(b"{not json"), "jumps.json"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert "导入失败" in response.get_data(as_text=True)


def test_import_rejects_bad_jump_target(client):
    jumps = dict(JUMPS, jumps={"Q1": {"租赁": "Q99"}})
    response = upload(client, jumps=jumps)
    assert "导入失败" in response.get_data(as_text=True)
