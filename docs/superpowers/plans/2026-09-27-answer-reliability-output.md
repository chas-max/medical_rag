# Reliable Qwen Answers and Compact Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Qwen the only answer provider, surface model failures safely, and display short, source-linked medical answers without debug clutter.

**Architecture:** Preserve the existing workflow DAG and public `answer`/`citations`/`abstained` response shape. Align all provider entry points with Qwen, translate known SDK errors at the transport boundary, and render verified claim lines in a focused React component. The chat page treats failed workflow runs as errors rather than assistant replies and keeps its advanced controls collapsed.

**Tech Stack:** Python 3.11+, FastAPI, OpenAI Python SDK, pytest; React 19, TypeScript, Vitest, Testing Library, CSS.

## Global Constraints

- Follow [approved design](../specs/2026-09-27-answer-reliability-output-design.md) and [Qwen-only design](../specs/2026-09-27-qwen-only-provider-design.md); keep `qwen3.8-flash` and the existing Qwen base URL.
- Preserve the existing `/answers` `{answer, citations, abstained}` and workflow run result shapes, and the published workflow DAG.
- Never read or edit `.env`, restore `.env.example`, expose raw upstream errors or model drafts, call paid models in tests, silently map DeepSeek requests to Qwen, or replace verified output with raw retrieval text.
- Preserve the pre-existing modifications in `api.py`, `generation.py`, `embeddings.py` and their tests. At every overlapping file, inspect `git diff` first and edit only task-specific hunks. Do not stage or commit pre-existing user hunks; if separation is uncertain, leave the work uncommitted and report it. The untracked logs and `outputs/` are out of scope.
- Use a red–green test cycle for each change. These steps assume PowerShell in `D:\project_rag`; frontend commands use `npm --prefix frontend`.

## File ownership and order

1. Runtime provider: `src/medical_rag/config.py`, `api.py`, `service.py`, `workflow/nodes/input.py`, `generation.py`; corresponding `tests/test_api.py`, `test_workflow_api.py`, `test_workflow_nodes.py`, `test_generation.py`.
2. Offline provider: `src/medical_rag/evaluate.py`, `tests/test_evaluate_cli.py`, `data/evaluations/medical-health-v1/README.md`.
3. Safe upstream failures and concise prompt: `generation.py`, `service.py`, `tests/test_generation.py`, `tests/test_workflow_api.py`.
4. Presentation-only formatting: create `frontend/src/components/AnswerContent.tsx` and `frontend/src/test/answer-content.test.tsx`; update `frontend/src/api/workflows.ts` and `frontend/src/styles.css`.
5. Chat/editor wiring: `frontend/src/pages/ChatPage.tsx`, `WorkflowEditorPage.tsx`, `frontend/src/test/pages.test.tsx`, `editor.test.tsx`, `frontend/src/styles.css`, `README.md`.

---

### Task 1: Qwen-only runtime entry points

**Interfaces:** `AnswerRequest.provider: Literal["qwen"] = "qwen"`; `query_input(...)["provider"] == "qwen"` by default; `OpenAITransport.complete(provider, system, user) -> str` accepts only `qwen`; `Settings` retains only `qwen_api_key`, `qwen_base_url`, `qwen_model` among provider settings. Downstream tasks keep the `provider` argument for compatibility.

- [ ] **Step 1: Write failing tests and update legacy success fixtures.** In `tests/test_api.py`, add:

```python
def test_default_answer_uses_qwen_and_deepseek_is_rejected():
    calls = []

    class CaptureService(FakeService):
        def answer(self, question, provider, top_k):
            calls.append(provider)
            return super().answer(question, provider, top_k)

    api = TestClient(create_app(service=CaptureService()))
    assert api.post("/answers", json={"question": "高血压"}).status_code == 200
    assert calls == ["qwen"]
    rejected = api.post("/answers", json={"question": "高血压", "provider": "deepseek"})
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "VALIDATION_ERROR"
```

In `tests/test_workflow_api.py`, add:

```python
def test_workflow_defaults_to_qwen_and_rejects_deepseek(tmp_path):
    api, _ = client(tmp_path)
    accepted = api.post("/workflows/medical-answer/runs", json={"inputs": {"question": "高血压"}})
    assert accepted.status_code == 200
    rejected = api.post("/workflows/medical-answer/runs", json={"inputs": {
        "question": "高血压", "provider": "deepseek"}})
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "INVALID_PROVIDER"
```

In `tests/test_generation.py`, replace the existing two-provider model-selection test with one Qwen-only test that retains the current `max_retries == 0` assertion:

```python
def test_transport_uses_only_configured_qwen_model():
    models, options = [], []

    class Completions:
        def create(self, **kwargs):
            models.append(kwargs["model"])
            return type("Response", (), {"choices": [type("Choice", (), {
                "message": type("Message", (), {"content": "{}"})()})()]})()

    class Client:
        def __init__(self, **kwargs):
            options.append(kwargs)
            self.chat = type("Chat", (), {"completions": Completions()})()

    transport = OpenAITransport(Settings(qwen_api_key="key"), client_factory=Client)
    assert transport.complete("qwen", "system", "user") == "{}"
    assert models == ["qwen3.8-flash"]
    assert options[0]["max_retries"] == 0
    with pytest.raises(ApiError) as rejected:
        transport.complete("deepseek", "system", "user")
    assert rejected.value.code == "INVALID_PROVIDER"
```

Change existing *successful* test calls with `"deepseek"` in `tests/test_generation.py`, `tests/test_workflow_nodes.py` and `tests/test_workflow_api.py` to `"qwen"`; keep explicit DeepSeek rejection tests. In particular, the existing workflow run fixture becomes:

```python
response = api.post("/workflows/medical-answer/runs", json={"inputs": {
    "question": "高血压", "provider": "qwen", "top_k": 5}})
```

Existing `test_validator_*` expectations, including the user's uncommitted whitespace/partial-claim cases, must remain unchanged.

- [ ] **Step 2: Verify RED.** Run `python -m pytest tests/test_api.py::test_default_answer_uses_qwen_and_deepseek_is_rejected tests/test_workflow_api.py::test_workflow_defaults_to_qwen_and_rejects_deepseek tests/test_generation.py::test_transport_uses_only_configured_qwen_model -q`. Expected: assertions fail because the API still defaults to DeepSeek and the transport accepts it.

- [ ] **Step 3: Implement only the provider changes.** Use these exact replacements, retaining unrelated current work:

```python
# config.py: remove deepseek_api_key/base_url/model; keep:
qwen_api_key: str = ""
qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
qwen_model: str = "qwen3.8-flash"

# api.py, AnswerRequest:
provider: Literal["qwen"] = "qwen"

# service.py, run_workflow input check:
if inputs.get("provider", "qwen") != "qwen":
    raise ApiError("INVALID_PROVIDER", "不支持的生成模型厂商", 422)

# workflow/nodes/input.py, query_input:
provider = inputs.get("provider", "qwen")
if provider != "qwen":
    raise ValueError("不支持的生成模型厂商")

# generation.py, OpenAITransport.complete before the client call:
if provider != "qwen":
    raise ApiError("INVALID_PROVIDER", "不支持的生成模型厂商", 422)
api_key = self.settings.qwen_api_key
if not api_key:
    raise ApiError("MODEL_NOT_CONFIGURED", "Qwen API Key 未配置", 503)
```

In the same transport, use the following client invocation instead of dynamic provider attributes; preserve the user's other transport options and do not touch the evidence validator:

```python
client = factory(api_key=api_key, base_url=self.settings.qwen_base_url,
                 timeout=60.0, max_retries=0)
response = client.chat.completions.create(
    model=self.settings.qwen_model,
    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    temperature=0,
)
return response.choices[0].message.content or ""
```

- [ ] **Step 4: Verify GREEN and inspect the diff.** Run `python -m pytest tests/test_api.py tests/test_workflow_api.py tests/test_workflow_nodes.py tests/test_generation.py -q`; expected: all focused tests pass. Run `git diff -- src/medical_rag/config.py src/medical_rag/api.py src/medical_rag/service.py src/medical_rag/workflow/nodes/input.py src/medical_rag/generation.py tests/test_api.py tests/test_workflow_api.py tests/test_workflow_nodes.py tests/test_generation.py`; expected: only provider-specific new hunks plus the pre-existing user edits. Do not commit inseparable pre-existing hunks.

### Task 2: Qwen-only offline evaluation

**Interfaces:** `main(argv, factory)` still accepts `--answer-provider` and `--judge-provider` but argparse choices/defaults are Qwen-only. `_live_dependencies(settings, answer_provider, judge_provider)` still accepts both arguments and checks the single configured Qwen key before creating Milvus/model clients. `evaluate_cases(...)` remains provider-neutral for direct unit tests.

- [ ] **Step 1: Add CLI regression tests.** In `tests/test_evaluate_cli.py`, add:

```python
def test_cli_defaults_both_providers_to_qwen(tmp_path):
    dataset = tmp_path / "dataset.jsonl"
    output = tmp_path / "report.json"
    dataset.write_text('{"question":"问题","relevant_chunk_ids":["id"]}', encoding="utf-8")
    providers = []

    def factory(settings, answer_provider, judge_provider):
        providers.append((answer_provider, judge_provider))
        return Service(), Judge()

    assert main(["--dataset", str(dataset), "--output", str(output)], factory=factory) == 0
    assert providers == [("qwen", "qwen")]
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["config"]["answer_provider"] == "qwen"
    assert report["config"]["judge_provider"] == "qwen"


def test_cli_rejects_each_legacy_provider_before_initialization(tmp_path):
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text('{"question":"问题","relevant_chunk_ids":["id"]}', encoding="utf-8")
    calls = []

    def factory(*args):
        calls.append(args)
        return Service(), Judge()

    prefix = ["--dataset", str(dataset), "--output", str(tmp_path / "report.json")]
    assert main(prefix + ["--answer-provider", "deepseek"], factory=factory) == 2
    assert main(prefix + ["--judge-provider", "deepseek"], factory=factory) == 2
    assert calls == []
```

- [ ] **Step 2: Verify RED.** Run `python -m pytest tests/test_evaluate_cli.py::test_cli_defaults_both_providers_to_qwen tests/test_evaluate_cli.py::test_cli_rejects_each_legacy_provider_before_initialization -q`; expected: wrong default and accepted legacy choices.

- [ ] **Step 3: Implement CLI restrictions and update its active example.** In `src/medical_rag/evaluate.py`, replace the key check, judge model selection and parser arguments with:

```python
if not settings.qwen_api_key:
    raise ValueError("Qwen API Key 未配置")
judge = EvaluationJudge(OpenAITransport(settings), judge_provider, settings.qwen_model)
parser.add_argument("--answer-provider", choices=("qwen",), default="qwen")
parser.add_argument("--judge-provider", choices=("qwen",), default="qwen")
```

Keep `answer_provider` and `judge_provider` parameters and their report fields. In `data/evaluations/medical-health-v1/README.md`, change the sample invocation to `--answer-provider qwen --judge-provider qwen` (or omit both flags with a sentence that both default to Qwen); do not edit archived specs or plans.

- [ ] **Step 4: Verify GREEN.** Run `python -m pytest tests/test_evaluate_cli.py tests/test_evaluation.py tests/test_evaluation_judge.py -q`; expected: all pass without live network. Review `git diff -- src/medical_rag/evaluate.py tests/test_evaluate_cli.py data/evaluations/medical-health-v1/README.md` before any commit.

### Task 3: Safe model failures and focused answer prompt

**Interfaces:** `OpenAITransport.complete(...)` raises `ApiError` with `MODEL_AUTH_FAILED` (503), `MODEL_RATE_LIMITED` (429), `MODEL_UNAVAILABLE` (503), or `MODEL_UPSTREAM_ERROR` (502) for known SDK errors. `RagService.run(...)` preserves these public statuses after the workflow trace. Successful output and the existing citation validator remain unchanged.

- [ ] **Step 1: Add failing transport and workflow tests.** In `tests/test_generation.py`, add imports `httpx`, `SimpleNamespace`, `AuthenticationError`, `PermissionDeniedError`, `RateLimitError`, `APIStatusError`, `APITimeoutError`, `APIConnectionError`; add:

```python
@pytest.mark.parametrize("error_type,status,code,http_status", [
    (AuthenticationError, 401, "MODEL_AUTH_FAILED", 503),
    (PermissionDeniedError, 403, "MODEL_AUTH_FAILED", 503),
    (RateLimitError, 429, "MODEL_RATE_LIMITED", 429),
    (APIStatusError, 500, "MODEL_UPSTREAM_ERROR", 502),
])
def test_transport_maps_upstream_status_without_exposing_body(error_type, status, code, http_status):
    request = httpx.Request("POST", "https://example.invalid/chat")
    upstream = httpx.Response(status, request=request)

    class Client:
        def __init__(self, **kwargs):
            def fail(**kwargs):
                raise error_type("secret upstream", response=upstream, body={"secret": "private"})
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=fail))

    transport = OpenAITransport(Settings(qwen_api_key="key"), client_factory=Client)
    with pytest.raises(ApiError) as raised:
        transport.complete("qwen", "system", "question")
    assert (raised.value.code, raised.value.status) == (code, http_status)
    assert "secret" not in raised.value.message
    assert "private" not in raised.value.message


@pytest.mark.parametrize("error", [
    APITimeoutError(request=httpx.Request("POST", "https://example.invalid/chat")),
    APIConnectionError(request=httpx.Request("POST", "https://example.invalid/chat")),
])
def test_transport_reports_network_failure_without_raw_error(error):
    class Client:
        def __init__(self, **kwargs):
            def fail(**kwargs):
                raise error
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=fail))

    with pytest.raises(ApiError) as raised:
        OpenAITransport(Settings(qwen_api_key="key"), client_factory=Client).complete("qwen", "s", "u")
    assert (raised.value.code, raised.value.status) == ("MODEL_UNAVAILABLE", 503)
```

Extend the existing `test_generation_prompt_limits_claim_count_to_validator_limit` with:

```python
assert "优先 3–5 条" in transport.system
assert "先回答问题" in transport.system
```

In `tests/test_workflow_api.py`, import `ApiError` and add:

```python
def test_model_rate_limit_stays_safe_in_both_answer_routes(tmp_path):
    class LimitedTransport:
        def complete(self, provider, system, user):
            raise ApiError("MODEL_RATE_LIMITED", "生成模型请求过于频繁，请稍后重试", 429)

    service = RagService(Settings(), MemoryStore(), Embedder(), Reranker(),
                         GroundedGenerator(LimitedTransport()), workflow_root=tmp_path)
    api = TestClient(create_app(service=service))
    uploaded = api.post("/documents", files={"file": (
        "guide.md", "# 高血压\n\n高血压患者应规律随访。".encode())})
    assert uploaded.status_code == 200
    answered = api.post("/answers", json={"question": "高血压", "provider": "qwen"})
    assert answered.status_code == 429
    assert answered.json()["error"]["code"] == "MODEL_RATE_LIMITED"
    run = api.post("/workflows/medical-answer/runs", json={"inputs": {
        "question": "高血压", "provider": "qwen", "top_k": 5}})
    assert run.status_code == 200
    assert run.json()["data"]["status"] == "failed"
    failure = next(item["error"] for item in run.json()["data"]["trace"]
                   if item["status"] == "failed")
    assert failure == {"code": "MODEL_RATE_LIMITED", "message": "生成模型请求过于频繁，请稍后重试"}
```

- [ ] **Step 2: Verify RED.** Run `python -m pytest tests/test_generation.py::test_transport_maps_upstream_status_without_exposing_body tests/test_generation.py::test_transport_reports_network_failure_without_raw_error tests/test_generation.py::test_generation_prompt_limits_claim_count_to_validator_limit tests/test_workflow_api.py::test_model_rate_limit_stays_safe_in_both_answer_routes -q`; expected: known SDK errors and prompt/status mapping fail; resolve only test setup errors before implementation.

- [ ] **Step 3: Implement error boundaries and prompt text.** Wrap the existing `client.chat.completions.create(...)` in `OpenAITransport.complete` with these handlers, in this order, while preserving its request arguments:

```python
except (AuthenticationError, PermissionDeniedError) as error:
    raise ApiError("MODEL_AUTH_FAILED", "生成模型认证或权限不足，请检查 Qwen 配置", 503) from error
except RateLimitError as error:
    raise ApiError("MODEL_RATE_LIMITED", "生成模型请求过于频繁，请稍后重试", 429) from error
except (APITimeoutError, APIConnectionError) as error:
    raise ApiError("MODEL_UNAVAILABLE", "生成模型服务不可用，请稍后重试", 503) from error
except APIStatusError as error:
    raise ApiError("MODEL_UPSTREAM_ERROR", "生成模型请求失败，请稍后重试", 502) from error
```

Import these SDK classes from `openai` in `generation.py`. In `RagService.run`, replace the existing status calculation with:

```python
status = {"MODEL_RATE_LIMITED": 429, "MODEL_UPSTREAM_ERROR": 502}.get(
    code, 422 if code in ("INVALID_DOCUMENT", "INVALID_SOURCE", "INVALID_QUERY", "INVALID_PROVIDER") else 503
)
```

Extend the prompt inside `GroundedGenerator.draft` with `"先回答问题的核心，再列出必要条件、数值或例外；优先 3–5 条，不重复，不添加证据之外的医学建议。"`; keep the existing 8-claim limit, JSON contract, quote requirement and validator unchanged. The user's current `draft` connection-error catch remains for injected transports.

- [ ] **Step 4: Verify GREEN.** Run `python -m pytest tests/test_generation.py tests/test_workflow_api.py tests/test_service.py -q`; expected: all pass, including the user's existing fallback/validation tests. Review `git diff -- src/medical_rag/generation.py src/medical_rag/service.py tests/test_generation.py tests/test_workflow_api.py` for accidental rewrites.

### Task 4: Safe citation rendering component

**Interfaces:** Create `AnswerContent({content, citations, abstained, onCitationClick})` with `content: string`, `citations: Citation[]`, `abstained: boolean`, `onCitationClick: (citation: Citation) => void`. The component returns text or structured claim lines and source buttons; it never parses HTML/Markdown. `Citation.chunk_id?: string` is added to the existing type.

- [ ] **Step 1: Write failing component tests.** Create `frontend/src/test/answer-content.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import AnswerContent from "../components/AnswerContent";

it("formats verified lines and opens consistently numbered citations", async () => {
  const open = vi.fn();
  const first = { chunk_id: "block-1", filename: "指南.md", excerpt: "原文" };
  const second = { chunk_id: "block-2", filename: "问答.pdf", excerpt: "另一段" };
  render(<AnswerContent content={"要点一 [block-1]\n要点二 [block-2] [block-1]"}
    citations={[first, second]} abstained={false} onCitationClick={open} />);
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(screen.queryByText(/block-1/)).not.toBeInTheDocument();
  const firstRefs = screen.getAllByRole("button", { name: "查看引用 1：指南.md" });
  expect(firstRefs).toHaveLength(2);
  await userEvent.click(firstRefs[1]);
  expect(open).toHaveBeenCalledWith(first);
});

it("preserves old text when citation metadata is unavailable", () => {
  render(<AnswerContent content="旧回答 [legacy]" citations={[{ filename: "旧资料" }]} abstained={false}
    onCitationClick={vi.fn()} />);
  expect(screen.getByText("旧回答 [legacy]")).toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Verify RED.** Run `npm --prefix frontend test -- src/test/answer-content.test.tsx`; expected: the component cannot be imported.

- [ ] **Step 3: Implement the component and type.** Add `chunk_id?: string` to `Citation` in `frontend/src/api/workflows.ts`. Create `frontend/src/components/AnswerContent.tsx`:

```tsx
import { Fragment } from "react";
import type { Citation } from "../api/workflows";

type Props = {
  content: string;
  citations: Citation[];
  abstained: boolean;
  onCitationClick: (citation: Citation) => void;
};

export default function AnswerContent({ content, citations, abstained, onCitationClick }: Props) {
  if (abstained || !citations.length) return <div className="message-text">{content}</div>;
  const sourceById = new Map<string, { citation: Citation; number: number }>();
  citations.forEach((citation) => {
    if (citation.chunk_id && !sourceById.has(citation.chunk_id)) {
      sourceById.set(citation.chunk_id, { citation, number: sourceById.size + 1 });
    }
  });
  if (!sourceById.size) return <div className="message-text">{content}</div>;
  const lines = content.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const renderLine = (line: string) => line.split(/(\[[^\[\]\n]+\])/g).map((part, index) => {
    const match = /^\[([^\[\]\n]+)\]$/.exec(part);
    const source = match ? sourceById.get(match[1]) : undefined;
    return source ? (
      <button key={index} type="button" className="citation-ref"
        aria-label={`查看引用 ${source.number}：${source.citation.filename || "资料"}`}
        onClick={() => onCitationClick(source.citation)}>[{source.number}]</button>
    ) : <Fragment key={index}>{part}</Fragment>;
  });
  if (lines.length <= 1) return <div className="message-text">{renderLine(lines[0] || content)}</div>;
  return <ol className="answer-list">{lines.map((line, index) =>
    <li key={index}>{renderLine(line)}</li>)}</ol>;
}
```

Add to `frontend/src/styles.css`:

```css
.answer-list { margin: 0; padding-left: 1.4em; color: #294637; line-height: 1.85; font-size: 14px; }
.answer-list li + li { margin-top: 10px; }
.citation-ref { border: 0; background: none; color: #276a4e; cursor: pointer; padding: 2px 4px; font: inherit; font-weight: 700; }
.citation-ref:focus-visible { outline: 2px solid #276a4e; border-radius: 4px; }
```

- [ ] **Step 4: Verify GREEN.** Run `npm --prefix frontend test -- src/test/answer-content.test.tsx`; expected: both tests pass. Run `npm --prefix frontend run lint`; expected: TypeScript and ESLint pass. Review `git diff -- frontend/src/api/workflows.ts frontend/src/styles.css` and the new component/test before any isolated commit.

### Task 5: Chat and editor integration, lean UI, active docs

**Interfaces:** The chat still calls `runWorkflow(workflowId, {question, provider: "qwen", top_k: topK})`, starts with `medical-answer`/5, and stores successful messages in the existing localStorage format. Failed run results produce a visible error alert but no assistant message. The workflow editor submits Qwen. Existing citations remain accessible by filename/page and excerpt.

- [ ] **Step 1: Extend frontend tests before changing pages.** In `frontend/src/test/pages.test.tsx`, add `localStorage.clear()` to `beforeEach` for isolation. Replace the existing success fixture's output with two independently cited claims, then replace its post-send assertions:

```tsx
output: {
  answer: "应定期复查 [chunk-1]\n复查结果需要记录 [chunk-2]",
  abstained: false,
  citations: [
    { chunk_id: "chunk-1", filename: "指南.md", excerpt: "应定期复查", page: 2 },
    { chunk_id: "chunk-2", filename: "指南.md", excerpt: "复查结果需要记录", page: 2 },
  ],
},
```

```tsx
expect(await screen.findByText("知源助手")).toBeInTheDocument();
expect(screen.getAllByRole("listitem")).toHaveLength(2);
expect(screen.queryByText(/\[chunk-1\]/)).not.toBeInTheDocument();
expect(screen.queryByText(/运行记录 #/)).not.toBeInTheDocument();
expect(screen.queryByRole("combobox", { name: "模型" })).not.toBeInTheDocument();
expect(screen.getByRole("button", { name: "来源 1：指南.md" })).toBeInTheDocument();
await user.click(screen.getByRole("button", { name: "查看引用 1：指南.md" }));
expect(screen.getByRole("heading", { name: "引用原文" })).toBeInTheDocument();
await waitFor(() => expect(api.runWorkflow).toHaveBeenCalledWith("medical-answer", {
  question: "如何复查？", provider: "qwen", top_k: 5,
}));
```

Add the failure test and a legacy-conversation test:

```tsx
it("shows failed model runs as an alert instead of an assistant answer", async () => {
  const user = userEvent.setup();
  vi.mocked(api.runWorkflow).mockResolvedValue({
    run_id: "failed-1", status: "failed", output: {},
    trace: [{ node_id: "llm", status: "failed", duration_ms: 1, metadata: {},
      error: { code: "MODEL_RATE_LIMITED", message: "生成模型请求过于频繁，请稍后重试" } }],
  });
  render(<MemoryRouter initialEntries={["/chat"]}><App /></MemoryRouter>);
  await user.type(screen.getByRole("textbox", { name: "输入医疗资料问题" }), "如何复查？");
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("生成模型请求过于频繁");
  expect(screen.queryByText("知源助手")).not.toBeInTheDocument();
  expect(screen.queryByText("证据不足")).not.toBeInTheDocument();
});

it("keeps old local conversations readable without citation metadata", async () => {
  localStorage.setItem("zhiyuan-conversations-v1", JSON.stringify([{
    id: "old", title: "旧问题", messages: [
      { role: "user", content: "旧问题" },
      { role: "assistant", content: "旧回答 [legacy]", citations: [{ filename: "旧资料" }],
        runId: "old-run" },
    ],
  }]));
  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={["/chat"]}><App /></MemoryRouter>);
  await user.click(screen.getByRole("button", { name: /旧问题/ }));
  expect(screen.getByText("旧回答 [legacy]")).toBeInTheDocument();
  expect(screen.queryByText(/运行记录 #/)).not.toBeInTheDocument();
});
```

In `frontend/src/test/editor.test.tsx`, extend the existing run assertion with `provider: "qwen"`. Add this advanced-controls test to `pages.test.tsx`:

```tsx
it("keeps workflow controls collapsed until requested", async () => {
  const user = userEvent.setup();
  vi.mocked(api.runWorkflow).mockResolvedValue({
    run_id: "run-2", status: "success",
    output: { answer: "资料不足", abstained: true, citations: [] }, trace: [],
  });
  render(<MemoryRouter initialEntries={["/chat"]}><App /></MemoryRouter>);
  expect(screen.queryByRole("combobox", { name: "证据数" })).not.toBeInTheDocument();
  await user.click(screen.getByText("高级设置"));
  await user.selectOptions(screen.getByRole("combobox", { name: "证据数" }), "8");
  await user.type(screen.getByRole("textbox", { name: "输入医疗资料问题" }), "高血压如何随访？");
  await user.click(screen.getByRole("button", { name: "发送问题" }));
  await waitFor(() => expect(api.runWorkflow).toHaveBeenCalledWith("medical-answer", {
    question: "高血压如何随访？", provider: "qwen", top_k: 8,
  }));
});
```

- [ ] **Step 2: Verify RED.** Run `npm --prefix frontend test -- src/test/pages.test.tsx src/test/editor.test.tsx`; expected: old provider, raw citation, failed-message and visible-controls assertions fail.

- [ ] **Step 3: Implement chat behavior.** In `frontend/src/pages/ChatPage.tsx`, remove provider state and selector; import `AnswerContent`. Keep the existing `Message.runId?` type for stored historical messages but stop writing or showing it. Add `const [failureNotice, setFailureNotice] = useState("");`, clear it on new chat and at the start of `send`, and replace the successful-result construction with:

```tsx
const result = await runWorkflow(workflowId, {
  question: text,
  provider: "qwen",
  top_k: topK,
});
if (result.status === "failed") {
  const failure = result.trace.find((item) => item.status === "failed")?.error;
  setFailureNotice(failure?.message || "回答失败，请检查服务后重试");
  return;
}
const message: Message = {
  role: "assistant",
  content: result.output.answer || "该工作流没有产生回答",
  citations: result.output.citations || [],
  abstained: result.output.abstained,
};
setConversations((previous) => previous.map((item) =>
  item.id === id ? { ...item, messages: [...item.messages, message] } : item));
```

In the existing `catch`, use `setFailureNotice(error instanceof Error ? error.message : "请求失败")`; leave upload status notices in `notice`. Add this error alert alongside the existing status notice:

```tsx
{failureNotice && (
  <div className="notice error" role="alert">
    {failureNotice}
    <button onClick={() => setFailureNotice("")} aria-label="关闭错误提示">×</button>
  </div>
)}
```

Replace message content and the source list with this JSX; keep historical `runId?` in the stored type but remove its rendered element:

```tsx
{message.role === "assistant" ? (
  <AnswerContent content={message.content} citations={message.citations || []}
    abstained={!!message.abstained} onCitationClick={setSelectedCitation} />
) : <div className="message-text">{message.content}</div>}
{message.role === "assistant" && !message.abstained && !!message.citations?.length && (
  <div className="citations">
    <span className="small-label">引用资料</span>
    {message.citations.map((citation, index) => (
      <button key={index} type="button" onClick={() => setSelectedCitation(citation)}
        aria-label={`来源 ${index + 1}：${citation.filename || citation.source || "资料"}`}>
        [{index + 1}] {citation.filename || citation.source || "资料"}
        {citation.page ? ` · 第 ${citation.page} 页` : ""}
      </button>
    ))}
  </div>
)}
```

- [ ] **Step 4: Hide advanced controls and update editor.** In `ChatPage.tsx`, remove only the model control; keep `workflowId` and `topK` states/defaults. Replace the always-visible settings JSX with:

```tsx
<details className="advanced-settings">
  <summary>高级设置</summary>
  <div className="settings-bar">
    <label>工作流{" "}
      <select aria-label="工作流" value={workflowId}
        onChange={(event) => setWorkflowId(event.target.value)}>
        {workflows.length ? workflows.map((item) => (
          <option key={item.id} value={item.id}>{item.name} · v{item.version}</option>
        )) : <option value="medical-answer">医疗资料问答</option>}
      </select>
    </label>
    <label>证据数{" "}
      <select aria-label="证据数" value={topK}
        onChange={(event) => setTopK(Number(event.target.value))}>
        {[3, 5, 8, 10].map((number) => (
          <option key={number} value={number}>{number}</option>
        ))}
      </select>
    </label>
    <span className="settings-spacer" />
    <Link to={`/workflows/${workflowId}`}>编辑此工作流 ↗</Link>
  </div>
</details>
```

In `WorkflowEditorPage.tsx`, replace `provider: "deepseek"` with `provider: "qwen"`. Add to `styles.css`:

```css
.advanced-settings { margin-bottom: 8px; font-size: 12px; color: #527862; }
.advanced-settings summary { cursor: pointer; width: fit-content; padding: 6px 0; }
.advanced-settings .settings-bar { padding-top: 8px; flex-wrap: wrap; }
.notice.error { color: #9a4435; background: #fff1ec; border-color: #f0d6cc; }
@media (max-width: 700px) {
  .advanced-settings .settings-bar { gap: 10px; }
  .citation-ref { min-width: 28px; min-height: 28px; }
}
```

Keep the medical disclaimer and evidence excerpt drawer. In `README.md`, replace all active DeepSeek model descriptions and samples with the following wording and commands; keep unrelated installation, safety and evaluation guidance unchanged:

```text
逐条引证问答使用 Qwen Flash；在 .env 中设置 QWEN_API_KEY，默认模型 ID 为 qwen3.8-flash。
问答页默认收起工作流和证据数高级设置；回答按要点展示，引用编号可打开来源原文，不显示运行 ID。
curl.exe -H "Content-Type: application/json" -d '{"question":"高血压如何随访？","provider":"qwen","top_k":5}' http://127.0.0.1:8000/answers
curl.exe -H "Content-Type: application/json" -d '{"inputs":{"question":"高血压如何随访？","provider":"qwen","top_k":5}}' http://127.0.0.1:8000/workflows/medical-answer/runs
python -m medical_rag.evaluate --dataset .\my-eval.jsonl --output .\my-report.json --top-k 5 --answer-provider qwen --judge-provider qwen
答案生成和评审均使用 Qwen，但仍会分别发起模型请求，可能分别计费，也可能存在同模型自评偏差。
```

Do not edit historical design documents.

- [ ] **Step 5: Verify GREEN.** Run `npm --prefix frontend test`; expected: all frontend tests pass. Run `npm --prefix frontend run lint` and `npm --prefix frontend run build`; expected: both exit 0. Review `git diff -- frontend/src/pages/ChatPage.tsx frontend/src/pages/WorkflowEditorPage.tsx frontend/src/test/pages.test.tsx frontend/src/test/editor.test.tsx frontend/src/styles.css README.md` and preserve unrelated edits.

## Final verification and safe handoff

- [ ] Run `python -m pytest -q --cov=medical_rag --cov-report=term-missing`, `python -m ruff check src tests`, and `python -m compileall -q src`; report exact totals and any pre-existing warning. Run `npm --prefix frontend test`, `npm --prefix frontend run lint`, and `npm --prefix frontend run build` again after the final edit.
- [ ] Run `rg -n -i 'deepseek' src frontend/src README.md data/evaluations/medical-health-v1/README.md`; expected: no active provider use; explicit rejection tests and archived specs may still mention it. Run `git diff --check` and `git status --short`, inspect staged and unstaged changes, and confirm `.env`, `.env.example`, user logs and `outputs/` are untouched.
- [ ] Do not claim a live Qwen answer was verified without a user-authorized paid API call and functioning Milvus. Report automated behavior verification separately from live connectivity.
