"""Start the RAG API with Loguru sinks installed before uvicorn takes over.

``python -m uvicorn medical_rag.api:app`` still works, but uvicorn's own logging
configuration replaces the root handler, so third-party records would bypass the
rotating files. This launcher passes ``log_config=None`` and keeps one sink set.
"""

import argparse
from collections.abc import Sequence

from loguru import logger

from .logging_setup import configure_logging, flush_logging


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="公开医疗资料检索问答服务")
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=8001)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    # Configure before importing the app so module-level startup warnings are kept.
    configure_logging("api")
    import uvicorn

    from .api import app

    logger.info(f"RAG API listening on {args.host}:{args.port}")
    try:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_config=None,
            access_log=False,
            proxy_headers=False,
            server_header=False,
        )
    finally:
        flush_logging()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
