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
    # provider 只是记录运维配置了哪一档模型，不再挑选凭据：任何非空名字都接受，
    # 但可用的字符串能让运行不在中途失败。校验由节点声明，HTTP 层据此回 422。
    if not isinstance(provider, str) or not provider.strip():
        raise ApiError("INVALID_PROVIDER", "provider 需要是非空字符串", 422)
    return {"question": question.strip(), "top_k": top_k, "provider": provider.strip()}


def document_input(inputs: dict, config: dict, context: ExecutionContext) -> dict:
    filename, content = inputs.get("filename"), inputs.get("content")
    if not isinstance(filename, str) or not isinstance(content, bytes):
        raise ValueError("上传文件参数无效")
    return {"filename": filename, "content": content, "source_id": inputs.get("source_id")}
