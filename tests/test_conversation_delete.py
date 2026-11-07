"""Deleting a conversation must remove every derived copy of it."""

import pytest
from document_helpers import upload_and_process
from test_chat_history import history_client

from medical_rag.chat_history import ChatHistoryStore
from medical_rag.memory import MemoryStore
from medical_rag.types import ApiError

CONVERSATION = "chat-1"


def _chat(api, question="如何随访？"):
    response = api.post("/chat/runs", json={
        "request_id": "request-1", "conversation_id": CONVERSATION,
        "inputs": {"question": question, "provider": "qwen", "top_k": 5}})
    assert response.status_code == 200, response.text
    return response.json()["data"]


# -- history ----------------------------------------------------------------

def test_delete_returns_the_runs_of_that_conversation(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.sqlite3")
    turn = store.begin_turn(CONVERSATION, "如何随访？")
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO chat_requests VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("request-1", CONVERSATION, "fingerprint", turn, "run-1", "completed", 0,
             None, None, "{}"))

    run_ids = store.delete_conversation(CONVERSATION)

    assert run_ids == ["run-1"]
    assert store.list_conversations() == []
    assert store.get_run("run-1") is None


def test_delete_unknown_conversation_is_none(tmp_path):
    assert ChatHistoryStore(tmp_path / "history.sqlite3").delete_conversation("missing") is None


def test_delete_refuses_while_an_answer_is_processing(tmp_path):
    store = ChatHistoryStore(tmp_path / "history.sqlite3")
    store.begin_request(CONVERSATION, "request-1", "如何随访？", "fingerprint")

    with pytest.raises(ApiError) as error:
        store.delete_conversation(CONVERSATION)

    assert (error.value.code, error.value.status) == ("REQUEST_IN_PROGRESS", 409)


# -- memory -----------------------------------------------------------------

def test_memory_cleanup_removes_jobs_episodes_and_pending_confirmations(tmp_path):
    store = MemoryStore(tmp_path / "history.sqlite3", tmp_path / "episodes")
    body = tmp_path / "episodes" / "run-1.md"
    body.parent.mkdir(parents=True, exist_ok=True)
    body.write_text("摘要", encoding="utf-8")
    with store._connect(True) as connection:
        connection.execute(
            "INSERT INTO memory_jobs(run_id,user_id,conversation_id,source_order,question,output,"
            "revision,consent,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("run-1", "local-user", CONVERSATION, 0, "如何随访？", "{}", 0, 0, "pending", "now"))
        connection.execute(
            "INSERT INTO memory_episodes(id,user_id,path,title,tags,created_at,"
            "source_conversation_id,source_run_id,sensitive,status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("e1", "local-user", "run-1.md", "随访", "[]", "now", CONVERSATION, "run-1", 0, "ready"))
        connection.execute(
            "INSERT INTO profile_confirmations(token_hash,user_id,request_id,conversation_id,"
            "profile_revision,privacy_revision,mutations,requires_consent,expires_at,status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("hash", "local-user", "request-1", CONVERSATION, 0, 0, "[]", 0, 0, "pending"))
        # 画像条目是用户的长期陈述，不应随会话删除。
        connection.execute(
            "INSERT INTO memory_profiles(key,user_id,category,value,sensitive,source,support,"
            "source_order,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("occupation", "local-user", "profile", "护士", 0, "assert", "我是一名护士",
             0, "now", "now"))

    removed = store.delete_conversation(CONVERSATION, ["run-1"])

    assert removed == {"jobs": 1, "episodes": 1, "confirmations": 1}
    assert not body.exists()
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_jobs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM memory_episodes").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM profile_confirmations").fetchone()[0] == 0
        assert connection.execute("SELECT value FROM memory_profiles WHERE key='occupation'").fetchone()[0] == "护士"


# -- end to end -------------------------------------------------------------

def test_delete_endpoint_removes_history_digest_and_run(tmp_path, monkeypatch):
    api, _ = history_client(tmp_path, monkeypatch)
    upload_and_process(api, files={"file": ("guide.md", "# 指南\n\n如何随访？应规律随访。".encode())})
    run = _chat(api)
    store = ChatHistoryStore(tmp_path / "history.sqlite3")
    store.save_summary(CONVERSATION, 1, "摘要")

    response = api.delete(f"/conversations/{CONVERSATION}")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["deleted"] is True
    # 测试里没有 Milvus 会话索引：向量状态是"未启用"，而不是假装删成功。
    assert data["vectors"] in {"disabled", "unavailable", "deleted"}
    assert api.get(f"/conversations/{CONVERSATION}").status_code == 404
    assert api.get(f"/runs/{run['run_id']}").status_code == 404
    assert store.summary(CONVERSATION) == (0, "")
    assert store.recent_turns(CONVERSATION) == []


def test_delete_unknown_conversation_returns_404(tmp_path, monkeypatch):
    api, _ = history_client(tmp_path, monkeypatch)

    response = api.delete("/conversations/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"
