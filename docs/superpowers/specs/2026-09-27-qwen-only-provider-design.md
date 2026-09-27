# Qwen-Only Model Provider Design

**Date:** 2026-09-27  
**Status:** Approved approach pending document review

## Goal

Remove DeepSeek as an active model provider and make Qwen the sole model for online answers, workflow runs, and both answer generation and judging in offline evaluation.

## Chosen approach

Keep the current provider-shaped request and CLI interfaces where they already exist, but constrain them to Qwen. This preserves the shape used by callers that already select Qwen while making DeepSeek requests invalid. Do not silently map a legacy `deepseek` value to Qwen.

## Behavior and interfaces

- Runtime settings contain only Qwen API key, base URL, and model configuration; retain the existing Qwen model default unless current project behavior proves otherwise.
- Answer APIs and workflow inputs default to `qwen` and accept no other provider. Existing provider fields remain, so Qwen callers need no request-shape migration.
- The chat page no longer offers a model selector and always submits Qwen. The workflow editor also submits Qwen for published runs.
- Offline evaluation uses Qwen for both answers and judging. Existing `--answer-provider` and `--judge-provider` flags remain for shape compatibility, default to Qwen, and accept only Qwen.
- Qwen connectivity/configuration failures continue to use existing error handling; no fallback to a different model is introduced.

## Documentation and configuration boundaries

- Update active user documentation and examples, including the root README and evaluation dataset README, to describe Qwen-only operation.
- Preserve old specs and plans as historical records rather than rewriting their original decisions.
- Do not read, modify, or delete `.env` secrets. Any leftover DeepSeek variable there will be unused by the application after this change.
- Preserve the existing uncommitted deletion of `.env.example`; do not restore it as part of this work.
- Preserve all pre-existing uncommitted code and test changes, editing only the necessary provider-related hunks.

## Testing

- Add or update backend tests first to prove Qwen is the only supported provider and remains the default for API and workflow execution.
- Update frontend tests to assert the chat has no model selector and workflow calls submit Qwen.
- Update evaluation CLI tests to prove both answer and judge provider defaults/choices are Qwen-only.
- Run the focused Python and frontend tests, then the broader project test suites and lint/build checks when practical. Tests must not call paid model APIs.
- Search active runtime code and current user documentation for remaining DeepSeek references; intentional legacy rejection tests and archived design/plan records are allowed.

## Non-goals

- Changing the Qwen model ID, endpoint, or model behavior beyond using the existing Qwen configuration.
- Changing embeddings, retrieval, Milvus data, or the retrieval-quality issue reported in the prior UI test.
- Cleaning secrets from the user's local `.env` file.
