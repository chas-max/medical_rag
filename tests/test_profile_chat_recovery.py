"""Message identity fencing and durable replay use a temporary history DB."""

import sqlite3
from contextlib import closing

import pytest

from medical_rag.chat_history import ChatHistoryStore
from medical_rag.types import ApiError


def test_crash_before_profile_commit_cannot_resurrect_after_forgetting(tmp_path, monkeypatch):
    from test_memory_api import memory_client
    from test_profile_chat_api import nurse_decision, payload

    from medical_rag.chat_api import ChatRunRequest

    class CrashOnce:
        calls = 0

        def analyze(self, *args):
            self.calls += 1
            if self.calls == 1:
                raise SystemExit("simulated process crash")
            return nurse_decision()

    analyzer = CrashOnce()
    _, app, _ = memory_client(tmp_path, monkeypatch, analyzer=analyzer)
    runtime = app.state.chat_runtime
    app.state.memory.save_profile("occupation", "教师", "background")
    request = ChatRunRequest(**payload())
    with pytest.raises(SystemExit):
        runtime.run(request)
    app.state.memory.delete_profile("occupation")
    with runtime.history._connect() as connection:
        connection.execute("UPDATE chat_requests SET lease_until=0")
    result = runtime.run(request)
    assert app.state.memory.state()["profiles"] == []
    assert result["profile"]["status"] == "conflict"
    assert analyzer.calls == 1


def test_committed_profile_recovers_after_crash_without_second_analysis(tmp_path, monkeypatch):
    from test_memory_api import memory_client
    from test_profile_chat_api import StaticAnalyzer, nurse_decision, payload

    from medical_rag.chat_api import ChatRunRequest

    analyzer = StaticAnalyzer(nurse_decision())
    _, app, _ = memory_client(tmp_path, monkeypatch, analyzer=analyzer)
    runtime = app.state.chat_runtime
    original = runtime.history.finish_turn

    def crash(*args, **kwargs):
        raise SystemExit("simulated crash after profile commit")

    monkeypatch.setattr(runtime.history, "finish_turn", crash)
    with pytest.raises(SystemExit):
        runtime.run(ChatRunRequest(**payload()))
    assert runtime.profiles.snapshot().profile_revision == 1
    monkeypatch.setattr(runtime.history, "finish_turn", original)
    with runtime.history._connect() as connection:
        connection.execute("UPDATE chat_requests SET lease_until=0")
    # Recreate app/service to prove recovery is not an in-process cache.
    _, restarted, _ = memory_client(tmp_path, monkeypatch, analyzer=analyzer)
    result = restarted.state.chat_runtime.run(ChatRunRequest(**payload()))
    assert result["profile"]["actions"][0]["status"] == "added"
    assert analyzer.calls == 1
    assert restarted.state.chat_runtime.profiles.snapshot().profile_revision == 1
    assert len(restarted.state.chat_runtime.history.get_conversation("c1")["messages"]) == 2


def test_stale_owner_cannot_write_profile_or_finish_after_takeover(tmp_path):
    from medical_rag.chat_history import ChatHistoryStore
    from medical_rag.memory import MemoryStore
    from medical_rag.profile_models import Mutation, ProfilePlan
    from medical_rag.profile_store import ProfileStore

    history = ChatHistoryStore(tmp_path / "history.sqlite3")
    profiles = ProfileStore(MemoryStore(history.path, tmp_path / "episodes"))
    first = history.begin_request("c1", "r1", "q", "fp")
    with history._connect() as connection:
        connection.execute("UPDATE chat_requests SET lease_until=0")
    second = history.begin_request("c1", "r1", "q", "fp")
    snapshot = profiles.snapshot()
    plan = ProfilePlan(snapshot.profile_revision, snapshot.privacy_revision,
                       mutations=(Mutation("occupation", "set", "护士", False, "auto", "我是护士"),))
    with pytest.raises(ApiError, match="接管"):
        profiles.apply("r1", "q", "c1", first["run_id"], first["turn_id"], plan, False,
                       owner_token=first["owner_token"])
    with pytest.raises(ApiError):
        history.finish_turn("c1", first["turn_id"], {"run_id": first["run_id"]},
                            request_id="r1", owner_token=first["owner_token"])
    assert profiles.snapshot().profiles == {}
    assert history.renew_request("r1", second["owner_token"])
    assert not history.renew_request("r1", first["owner_token"])


def test_live_request_renews_lease_and_duplicate_cannot_steal_it(tmp_path, monkeypatch):
    import threading

    from test_memory_api import memory_client
    from test_profile_chat_api import nurse_decision, payload

    from medical_rag import chat_runtime
    from medical_rag.chat_api import ChatRunRequest

    release, renewed = threading.Event(), threading.Event()

    class SlowAnalyzer:
        def analyze(self, *args):
            assert release.wait(2)
            return nurse_decision()

    monkeypatch.setattr(chat_runtime, "LEASE_RENEW_SECONDS", 0.01)
    _, app, _ = memory_client(tmp_path, monkeypatch, analyzer=SlowAnalyzer())
    runtime = app.state.chat_runtime
    original = runtime.history.renew_request

    def record_renewal(*args):
        result = original(*args)
        renewed.set()
        return result

    monkeypatch.setattr(runtime.history, "renew_request", record_renewal)
    results = []
    worker = threading.Thread(target=lambda: results.append(runtime.run(ChatRunRequest(**payload()))))
    worker.start()
    try:
        assert renewed.wait(2)
        with pytest.raises(ApiError) as error:
            runtime.run(ChatRunRequest(**payload()))
        assert error.value.code == "REQUEST_IN_PROGRESS"
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert results[0]["status"] == "success"


def test_begin_request_is_fenced_and_recovers_same_turn(tmp_path):
    history = ChatHistoryStore(tmp_path / "chat.sqlite3")
    first = history.begin_request("c1", "r1", "问题", "fingerprint")
    assert first["state"] == "new"
    with pytest.raises(ApiError):
        history.begin_request("c1", "r1", "问题", "different")
    with pytest.raises(ApiError):
        history.begin_request("c1", "r1", "问题", "fingerprint")
    with closing(sqlite3.connect(history.path)) as connection, connection:
        connection.execute("UPDATE chat_requests SET lease_until=0")
    second = history.begin_request("c1", "r1", "问题", "fingerprint")
    assert second["state"] == "recoverable"
    assert second["turn_id"] == first["turn_id"]
    assert second["run_id"] == first["run_id"]
    assert second["owner_token"] != first["owner_token"]
    assert len(history.get_conversation("c1")["messages"]) == 1
