"""Workflow input handlers."""

from ..contracts import ExecutionContext


def query_input(inputs: dict, config: dict, context: ExecutionContext) -> dict:
    question = inputs.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("问题不能为空")
    top_k = inputs.get("top_k", 5)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 20:
        raise ValueError("top_k 必须在 1 到 20 之间")
    provider = inputs.get("provider", "qwen")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider 需要是非空字符串")
    return {"question": question.strip(), "top_k": top_k, "provider": provider.strip()}


def document_input(inputs: dict, config: dict, context: ExecutionContext) -> dict:
    filename, content = inputs.get("filename"), inputs.get("content")
    if not isinstance(filename, str) or not isinstance(content, bytes):
        raise ValueError("上传文件参数无效")
    return {"filename": filename, "content": content, "source_id": inputs.get("source_id")}
