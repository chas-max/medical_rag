"""Explicit public error catalogue: never expose exception-provided text.

An exception code is only a lookup key; neither its message nor its claimed
status is authoritative. Unknown and malformed codes fail closed.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PublicError:
    code: str
    message: str
    status: int

    def data(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


_ERRORS = {
    "NOT_READY": ("服务尚未初始化", 503),
    "REQUEST_ID_CONFLICT": ("请求标识已用于不同内容", 409),
    "REQUEST_IN_PROGRESS": ("相同请求仍在处理中，请稍后重试", 409),
    "REQUEST_LEASE_LOST": ("请求已由其他进程接管，请重试读取结果", 409),
    "HISTORY_STORAGE_ERROR": ("历史记录暂不可用", 503),
    "CONVERSATION_NOT_FOUND": ("对话记录不存在", 404),
    "INVALID_QUERY": ("请求问题或参数不符合要求", 422),
    "INVALID_PROVIDER": ("模型服务提供方不符合要求", 422),
    "INVALID_SOURCE": ("资料来源标识无效", 422),
    "INVALID_DOCUMENT": ("上传的资料不符合要求", 422),
    "FILE_TOO_LARGE": ("文件超过上传大小限制", 413),
    "UNSUPPORTED_FORMAT": ("仅支持 Markdown、TXT 和可提取文字的 PDF", 415),
    "CHUNK_NOT_FOUND": ("未找到该资料块", 404),
    "NOT_FOUND": ("资源不存在", 404),
    "JOB_NOT_FOUND": ("任务不存在", 404),
    "JOB_STORAGE_ERROR": ("任务存储暂不可用", 503),
    "RUN_NOT_FOUND": ("运行记录不存在", 404),
    "MEMORY_STORAGE_ERROR": ("长期记忆存储暂不可用", 503),
    "MEMORY_PATH_ERROR": ("情景文件路径不安全", 503),
    "MEMORY_FILE_ERROR": ("情景文件无法读取", 503),
    "MEMORY_NOT_FOUND": ("情景记忆不存在或未保存完成", 404),
    "INVALID_MEMORY": ("画像请求不符合要求", 422),
    "MEMORY_CONSENT_REQUIRED": ("敏感画像保存需要本次授权", 422),
    "MEMORY_CONFIRMATION_INVALID": ("画像确认不存在或不属于本会话", 409),
    "MEMORY_CONFIRMATION_EXPIRED": ("画像确认已过期、取消或处理，请重新发起", 409),
    "MEMORY_CONFIRMATION_CONFLICT": ("画像已变更，请重新发起确认", 409),
    "MODEL_AUTH_FAILED": ("生成模型认证或权限不足，请检查模型配置", 503),
    "MODEL_RATE_LIMITED": ("生成模型请求过于频繁，请稍后重试", 429),
    "MODEL_UNAVAILABLE": ("生成模型服务不可用，请稍后重试", 503),
    "MODEL_UPSTREAM_ERROR": ("生成模型请求失败，请稍后重试", 502),
    "MODEL_NOT_CONFIGURED": ("模型 API Key 未配置", 503),
    "MODEL_CONFIG_INCOMPLETE": ("模型配置不完整，请补齐后重试", 422),
    "MODEL_CONFIG_INVALID_URL": ("Base URL 格式不符合要求", 422),
    "MODEL_CONFIG_AUTH_FAILED": ("模型认证失败，请检查配置", 422),
    "MODEL_CONFIG_RATE_LIMITED": ("模型服务正在限流，请稍后重试", 422),
    "MODEL_CONFIG_UNREACHABLE": ("无法连接模型服务，请检查配置", 422),
    "MODEL_CONFIG_MODEL_NOT_FOUND": ("模型不存在，请检查配置", 422),
    "MODEL_CONFIG_REJECTED": ("模型服务拒绝配置，请检查后重试", 422),
    "MODEL_PROFILE_NOT_FOUND": ("该模型配置不存在，请刷新后重试", 404),
    "WEB_SEARCH_NOT_CONFIGURED": ("联网搜索未配置", 503),
    "WEB_SEARCH_UNAVAILABLE": ("联网搜索服务不可用，请稍后重试", 503),
    "WEB_SEARCH_UPSTREAM_ERROR": ("联网搜索服务暂时失败，请稍后重试", 502),
    "WEB_SEARCH_AUTH_FAILED": ("联网搜索认证或权限不足，请检查配置", 503),
    "WEB_SEARCH_RATE_LIMITED": ("联网搜索请求过于频繁，请稍后重试", 429),
    "WEB_SEARCH_INVALID_REQUEST": ("联网搜索请求不被接受", 422),
    "WORKFLOW_REQUIRED": ("请先选择已发布的问答工作流", 422),
    "WORKFLOW_NOT_FOUND": ("工作流不存在", 404),
    "WORKFLOW_NOT_PUBLISHED": ("仅可运行已发布的工作流", 409),
    "WORKFLOW_CONFLICT": ("工作流状态冲突，请刷新后重试", 409),
    "INVALID_WORKFLOW": ("工作流定义无效", 422),
    "INVALID_WORKFLOW_KIND": ("迭代子图仅可由容器运行", 422),
}
_FALLBACK = PublicError("DEPENDENCY_ERROR", "聊天服务暂不可用", 503)


def public_error(code: object) -> PublicError:
    """Resolve only exact built-in strings in the catalogue."""
    if type(code) is not str or code not in _ERRORS:
        return _FALLBACK
    message, status = _ERRORS[code]
    return PublicError(code, message, status)


_HTTP_MESSAGES = {
    400: "请求无效", 401: "身份认证失败", 403: "无权访问", 404: "资源不存在",
    405: "请求方法不受支持", 409: "请求状态冲突", 422: "请求参数不符合要求",
    429: "请求过于频繁，请稍后重试", 500: "服务暂不可用", 503: "服务暂不可用",
}


def public_http_error(status: object) -> PublicError:
    """Map framework HTTP statuses without rendering untrusted detail."""
    if type(status) is not int or status not in _HTTP_MESSAGES:
        return _FALLBACK
    return PublicError("HTTP_ERROR", _HTTP_MESSAGES[status], status)
