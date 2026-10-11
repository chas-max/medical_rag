"""Workflow input handlers.

运行入参的形状与校验由输入节点自己声明：HTTP 层不再按工作流 ID 决定
哪些字段必填，而是把节点抛出的请求类错误码映射回 4xx。
"""

from ...types import ApiError
from ..contracts import ExecutionContext


def query_input(inputs: dict, config: dict, context: ExecutionContext) -> dict:
    question = inputs.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ApiError("INVALID_QUERY", "问题不能为空", 422)
    top_k = inputs.get("top_k", 5)
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 20:
        raise ApiError("INVALID_QUERY", "top_k 必须在 1 到 20 之间", 422)
    provider = inputs.get("provider", "qwen")
    if provider != "qwen":
        raise ApiError("INVALID_PROVIDER", "不支持的生成模型厂商", 422)
    return {"question": question.strip(), "top_k": top_k, "provider": provider}


def document_input(inputs: dict, config: dict, context: ExecutionContext) -> dict:
    filename, content = inputs.get("filename"), inputs.get("content")
    if not isinstance(filename, str) or not isinstance(content, bytes):
        raise ValueError("上传文件参数无效")
    return {"filename": filename, "content": content, "source_id": inputs.get("source_id")}
