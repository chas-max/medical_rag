"""Local profiles, atomic episode files, ordering and forgetting semantics."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from medical_rag.types import ApiError


@pytest.fixture
def store(tmp_path):
    from medical_rag.memory import MemoryStore
    return MemoryStore(tmp_path / "history.sqlite3", tmp_path / "episodes")


def enqueue(store, run="run1", order=1, question="我喜欢简短中文回答", consent=True, abstained=False):
    revision, _ = store.snapshot(question)
    with closing(sqlite3.connect(store.path)) as connection, connection:
        store.enqueue(connection, run, "../unsafe-chat", order, question,
                      {"answer": "讨论随访", "abstained": abstained}, revision, consent)


def candidates(value="简短中文", sensitive=False):
    return {"profiles": [{"key": "response_style", "value": value,
        "category": "preferences", "support": f"我喜欢{value}回答", "subject": "user",
        "sensitive": sensitive}], "episode": {"title": "随访讨论", "summary": "讨论了随访安排",
        "events": [], "decisions": [], "unresolved": ["下次随访"], "followups": [],
        "tags": ["随访"], "sensitive": sensitive}}


def episode_candidates(value="简短中文", sensitive=False):
    return {"episode": candidates(value, sensitive)["episode"]}


def complete_legacy(store, raw):
    job = store.claim()
    job["contract_version"] = 1
    store.complete(job, raw)


def test_profile_roundtrip_and_disable(store):
    store.save_profile("response_style", "简短中文", "preferences")
    reopened = type(store)(store.path, store.root)
    assert reopened.state()["profiles"][0]["value"] == "简短中文"
    assert "简短中文" in reopened.snapshot("如何随访")[1]
    reopened.set_enabled(False)
    assert store.snapshot("随访")[1] == ""
    reopened.set_enabled(True)
    assert "简短中文" in store.snapshot("随访")[1]


def test_episode_markdown_is_authoritative_and_recalled_by_relevance(store):
    enqueue(store)
    job = store.claim()
    store.complete(job, episode_candidates())
    state = store.state()
    assert state["jobs"][0]["status"] == "saved"
    episode = state["episodes"][0]
    files = list(store.root.rglob("*.md"))
    assert len(files) == 1
    assert files[0].resolve().is_relative_to(store.root.resolve())
    content = store.read_episode(episode["id"])["content"]
    assert "run1" in content and "随访讨论" in content and "local-user" in content
    assert "讨论了随访安排" in store.snapshot("随访")[1]
    assert "讨论了随访安排" not in store.snapshot("天文学黑洞")[1]
    assert "讨论了随访安排" not in store.snapshot("如何解决天文学问题")[1]
    reopened = type(store)(store.path, store.root)
    assert reopened.read_episode(episode["id"])["content"] == content
    assert reopened.claim() is None


@pytest.mark.parametrize("action", ["clear", "delete", "disable"])
def test_older_inflight_job_cannot_resurrect_memory(store, action):
    store.save_profile("response_style", "旧偏好", "preferences")
    enqueue(store)
    job = store.claim()
    if action == "clear":
        store.clear()
    elif action == "delete":
        store.delete_profile("response_style")
    elif action == "disable":
        store.set_enabled(False)
    store.complete(job, episode_candidates())
    state = store.state()
    assert state["episodes"] == []
    assert list(store.root.rglob("*.md")) == []
    assert not any(item["value"] == "简短中文" for item in state["profiles"])
    assert state["jobs"][0]["status"] == "cancelled"


def test_manual_profile_edit_does_not_cancel_unrelated_episode(store):
    enqueue(store)
    job = store.claim()
    store.save_profile("response_style", "新偏好", "preferences")
    store.complete(job, episode_candidates())
    assert len(store.state()["episodes"]) == 1
    assert store.state()["profiles"][0]["value"] == "新偏好"
    assert store.job_status("run1")["status"] == "saved"


def test_clear_removes_files_and_does_not_delete_original_history(store):
    enqueue(store)
    store.complete(store.claim(), episode_candidates())
    store.clear()
    assert store.state()["profiles"] == store.state()["episodes"] == []
    assert list(store.root.rglob("*.md")) == []
    assert store.state()["enabled"] is True


def test_out_of_order_jobs_and_manual_values_win(store):
    enqueue(store, "older", 1)
    older = store.claim()
    enqueue(store, "newer", 2, "我喜欢详细中文回答")
    newer = store.claim()
    store.complete(newer, episode_candidates("详细中文"))
    store.complete(older, episode_candidates())
    assert store.state()["profiles"] == []
    assert len(store.state()["episodes"]) == 2
    store.save_profile("response_style", "人工偏好", "preferences")
    enqueue(store, "after-manual", 3)
    store.complete(store.claim(), episode_candidates())
    assert store.state()["profiles"][0]["value"] == "人工偏好"


@pytest.mark.parametrize("question,change,consent", [
    ("我喜欢简短中文回答", {}, False),
    ("我喜欢简短中文回答", {"subject": "relative"}, False),
    ("我喜欢简短中文回答", {"support": "助手说我喜欢简短中文回答"}, False),
    ("假设我喜欢简短中文回答", {}, False),
    ("我喜欢简短中文回答", {"sensitive": True}, False),
    ("我喜欢简短中文回答", {"sensitive": True}, True),
    ("我喜欢简短中文回答", {"key": "unknown"}, False),
    ("我喜欢简短中文回答", {"value": "未说过的偏好"}, False),
])
def test_legacy_background_profiles_are_ignored_regardless_of_support(store, question, change, consent):
    enqueue(store, question=question, consent=consent)
    raw = candidates()
    raw["profiles"][0].update(change)
    raw["episode"] = None
    complete_legacy(store, raw)
    assert store.state()["profiles"] == []


def test_sensitive_question_cannot_leak_through_summary(store):
    enqueue(store, question="我正在服用阿司匹林", consent=False)
    raw = candidates()
    raw["profiles"] = []
    raw["episode"]["summary"] = "我正在服用阿司匹林"
    raw["episode"]["sensitive"] = False  # Misclassification is not consent.
    complete_legacy(store, raw)
    assert store.state()["episodes"] == []


def test_abstention_stores_neither_profiles_nor_success_episode(store):
    enqueue(store, abstained=True)
    complete_legacy(store, candidates())
    assert store.state()["profiles"] == []
    assert store.state()["episodes"] == []


def test_claim_is_atomic_and_same_run_is_idempotent(store):
    enqueue(store)
    enqueue(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(lambda _: store.claim(), range(2)))
    assert sum(job is not None for job in jobs) == 1
    store.complete(next(job for job in jobs if job), episode_candidates())
    assert len(store.state()["episodes"]) == 1


def test_corrupt_and_oversized_episode_skips_recall(store):
    enqueue(store)
    store.complete(store.claim(), episode_candidates())
    file = next(store.root.rglob("*.md"))
    file.write_text("invalid", encoding="utf-8")
    assert "讨论了随访安排" not in store.snapshot("随访")[1]
    with pytest.raises(ApiError):
        store.read_episode(store.state()["episodes"][0]["id"])
    file.write_text("x" * 33000, encoding="utf-8")
    assert "讨论了随访安排" not in store.snapshot("随访")[1]


def test_recall_and_profile_validation_are_bounded(store):
    for key in ["display_name", "occupation", "language", "response_style", "interests", "goals"]:
        category = {"display_name": "background", "occupation": "background", "language": "preferences",
                    "response_style": "preferences", "interests": "interests", "goals": "interests"}[key]
        store.save_profile(key, "偏好" * 240, category)
    assert len(store.snapshot("随访")[1]) <= 5000
    with pytest.raises(ApiError):
        store.save_profile("response_style", "x" * 501, "preferences")
    with pytest.raises(ApiError):
        store.save_profile("response_style", "中文", "health")
    with pytest.raises(ApiError):
        store.save_profile("health", "高血压", "health")


def test_invalid_model_schema_fails_without_partial_writes(store):
    enqueue(store)
    with pytest.raises(ValueError):
        store.complete(store.claim(), {"profiles": [], "episode": None, "sql": "DROP TABLE"})
    assert store.state()["profiles"] == store.state()["episodes"] == []


def test_delete_episode_invalidates_old_job_and_removes_markdown(store):
    enqueue(store)
    store.complete(store.claim(), episode_candidates())
    episode = store.state()["episodes"][0]
    enqueue(store, "later", 2)
    inflight = store.claim()
    store.delete_episode(episode["id"])
    store.complete(inflight, episode_candidates())
    assert store.state()["episodes"] == []
    assert list(store.root.rglob("*.md")) == []


def test_clear_file_failure_reports_error_without_claiming_success(store, monkeypatch):
    from pathlib import Path

    enqueue(store)
    store.complete(store.claim(), episode_candidates())
    original = Path.unlink

    def denied(path, *args, **kwargs):
        if path.suffix == ".md":
            raise PermissionError("denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(ApiError, match="长期记忆存储不可用"):
        store.clear()
    assert len(store.state()["episodes"]) == 1
    assert len(list(store.root.rglob("*.md"))) == 1


def test_file_write_failure_leaves_only_pending_index_not_profiles(store, monkeypatch):
    enqueue(store)
    job = store.claim()

    def broken(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "_write", broken)
    with pytest.raises(ApiError):
        store.complete(job, episode_candidates())
    assert store.state()["profiles"] == []
    assert "讨论了随访安排" not in store.snapshot("随访")[1]
    assert store.state()["episodes"] == []  # Only ready files are listed to users.
    store.clear()
    assert store.state()["episodes"] == []


def test_tampered_episode_path_cannot_read_or_delete_outside_root(store, tmp_path):
    enqueue(store)
    store.complete(store.claim(), episode_candidates())
    outside = tmp_path / "private.md"
    outside.write_text("private", encoding="utf-8")
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("UPDATE memory_episodes SET path='../private.md'")
    assert "private" not in store.snapshot("private")[1]
    episode = store.state()["episodes"][0]
    with pytest.raises(ApiError, match="情景文件路径不安全"):
        store.read_episode(episode["id"])
    with pytest.raises(ApiError):
        store.delete_episode(episode["id"])
    assert outside.read_text(encoding="utf-8") == "private"


def test_metadata_mismatch_is_not_recalled(store):
    enqueue(store)
    store.complete(store.claim(), episode_candidates())
    file = next(store.root.rglob("*.md"))
    file.write_text('<!-- memory-v1\n' + json.dumps({"version": 1, "id": "wrong", "user_id": "other"}) +
                    '\n-->\n# 随访\nwrong', encoding="utf-8")
    assert "wrong" not in store.snapshot("随访")[1]


def test_consent_marks_personal_health_episode_sensitive_even_if_model_misses_it(store):
    enqueue(store, question="我正在服用阿司匹林", consent=True)
    raw = candidates()
    raw["profiles"] = []
    raw["episode"]["summary"] = "用户自述正在服用阿司匹林"
    raw["episode"]["sensitive"] = False
    complete_legacy(store, raw)
    assert store.state()["episodes"][0]["sensitive"]


@pytest.mark.parametrize("question,key,value,support", [
    ("I take metformin daily", "interests", "metformin", "I take metformin daily"),
    ("我有哮喘", "interests", "哮喘", "我有哮喘"),
    ("我喜欢哮喘内容", "response_style", "哮喘", "我喜欢哮喘内容"),
    ("我叫哮喘患者", "display_name", "哮喘患者", "我叫哮喘患者"),
])
def test_unconsented_unknown_health_details_fail_closed(store, question, key, value, support):
    enqueue(store, question=question, consent=False)
    raw = candidates()
    raw["profiles"][0].update(key=key, value=value, support=support,
                                category={"interests": "interests", "response_style": "preferences", "display_name": "background"}[key])
    raw["episode"]["summary"] = question
    complete_legacy(store, raw)
    assert store.state()["profiles"] == store.state()["episodes"] == []
    assert store.job_status("run1")["status"] == "skipped"


def test_unconsented_generic_episode_never_copies_freeform_model_summary(store):
    enqueue(store, question="请介绍哮喘的一般知识", consent=False)
    raw = candidates()
    raw["episode"]["summary"] = "用户每日服用metformin并患有哮喘"
    complete_legacy(store, raw)
    episode = store.state()["episodes"][0]
    content = store.read_episode(episode["id"])["content"]
    assert "哮喘" in content
    assert "metformin" not in content


@pytest.mark.parametrize("question,flag", [
    ("请介绍李小红精神分裂症的一般知识", False),
    ("请介绍哮喘的一般知识", True),
    ("请介绍罕见未分类健康问题的一般知识", False),
])
def test_unconsented_personal_or_unknown_topics_and_sensitive_veto_are_not_saved(store, question, flag):
    enqueue(store, question=question, consent=False)
    raw = candidates()
    raw["profiles"] = []
    raw["episode"]["sensitive"] = flag
    complete_legacy(store, raw)
    assert store.state()["episodes"] == []
    assert store.job_status("run1")["status"] == "skipped"


def test_empty_candidates_and_protected_manual_values_report_skipped(store):
    enqueue(store, abstained=True)
    store.complete(store.claim(), {"episode": None})
    assert store.job_status("run1")["status"] == "skipped"
    store.save_profile("response_style", "人工值", "preferences")
    enqueue(store, "protected", 2, abstained=True)
    store.complete(store.claim(), episode_candidates())
    assert store.job_status("protected")["status"] == "skipped"


def test_unconsented_preference_quote_does_not_retain_unrelated_health_text(store):
    question = "我有哮喘，我喜欢简短中文回答"
    enqueue(store, question=question, consent=False)
    raw = candidates()
    raw["profiles"][0]["support"] = question
    complete_legacy(store, raw)
    assert store.state()["profiles"] == []
    content = store.read_episode(store.state()["episodes"][0]["id"])["content"]
    assert "哮喘" not in content


def test_authorized_health_episode_is_sensitive_without_profile_write(store):
    enqueue(store, question="我有哮喘", consent=True)
    raw = candidates()
    raw["profiles"][0].update(key="health", category="health", value="哮喘", support="我有哮喘", sensitive=False)
    complete_legacy(store, raw)
    assert store.state()["profiles"] == []
    assert store.state()["episodes"][0]["sensitive"]


def test_clear_removes_crash_leftover_temp_and_original_history_survives(store):
    from medical_rag.chat_history import ChatHistoryStore

    history = ChatHistoryStore(store.path)
    turn = history.begin_turn("chat", "问题")
    history.finish_turn("chat", turn, {"run_id": "chat-run", "status": "success",
                                       "output": {"answer": "回答"}, "trace": []})
    enqueue(store)
    store.complete(store.claim(), episode_candidates())
    file = next(store.root.rglob("*.md"))
    temporary = file.with_name(f".memory-{file.stem}.tmp")
    temporary.write_text("崩溃前写入的敏感片段", encoding="utf-8")
    store.clear()
    assert not temporary.exists()
    assert history.get_conversation("chat")["messages"][1]["content"] == "回答"


@pytest.mark.parametrize("question,value", [
    ("My mother has asthma", "asthma"), ("If I have asthma", "asthma"),
    ("我会得糖尿病吗？", "糖尿病"), ("我没有哮喘", "哮喘"),
    ("I do not have asthma", "asthma"), ("I think my mother has asthma", "asthma"),
    ('The doctor said "I have asthma"', "asthma"),
])
def test_sensitive_consent_does_not_prove_positive_self_report(store, question, value):
    enqueue(store, question=question, consent=True)
    raw = candidates()
    raw["profiles"][0].update(key="health", value=value, category="health", support=question, sensitive=True)
    raw["episode"] = None
    complete_legacy(store, raw)
    assert store.state()["profiles"] == []
    assert store.job_status("run1")["status"] == "skipped"
