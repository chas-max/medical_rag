# Streamlit 问答/指标控制台的最小运行镜像。
#
# 刻意不安装 torch / sentence-transformers / 项目其余依赖：界面只调用 RAG 后端
# 的 HTTP API 并读取既有 data/ 与 outputs/ 产物做展示，不参与检索或推理。
# 注意：容器内 127.0.0.1 指向容器自身，而 RAG 后端只接受回环来源，因此容器版
# 无法完成真实问答；它适合离线查看已持久化的指标与评测报告。-slim 基础镜像 +
# 纯 wheel 依赖已经够用，无需 build-essential/gcc。
FROM python:3.11-slim

# PYTHONPATH 让 /app/src 下的 medical_rag 包可直接 import；禁用 .pyc 写入，
# 因为容器以 read_only 根文件系统运行；/tmp 由 compose 提供 tmpfs，作为
# HOME，供 Streamlit 在只读根分区下存放临时配置与缓存。
ENV PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_FILE_WATCHER_TYPE=none

WORKDIR /app

# 仅最小运行依赖：Streamlit 自带 pandas，应用另需 httpx（后端调用）与
# pydantic-settings/loguru（项目配置/日志）。
RUN pip install --no-cache-dir \
        "streamlit>=1.40,<2" \
        "httpx>=0.27,<0.29" \
        "pydantic-settings>=2" \
        "loguru>=0.7"

COPY src ./src

# 非 root 运行（uid 10001）。/app/data/streamlit 必须归属 app：
# compose 的命名卷首次挂载时按镜像内目录属主初始化，否则界面无法写状态库。
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data/streamlit /app/outputs \
    && chown -R app:app /app

USER app

EXPOSE 8501

# 只打本机回环的 /_stcore/health，返回 ok 即健康。
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import sys, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3); sys.exit(0 if r.status == 200 and r.read().strip() == b'ok' else 1)"

# 容器内必须监听 0.0.0.0：Docker 端口映射转发到容器网卡地址，而不是容器
# 回环；宿主机侧 compose 已把发布端口限制在 127.0.0.1，不会暴露到局域网。
# 这里直接调用 Streamlit CLI，绕过启动器“仅允许回环”的本地校验。
CMD ["python", "-m", "streamlit", "run", "/app/src/medical_rag/ui/app.py", \
     "--server.address", "0.0.0.0", "--server.port", "8501", \
     "--server.headless", "true", "--server.fileWatcherType", "none", \
     "--browser.gatherUsageStats", "false"]
