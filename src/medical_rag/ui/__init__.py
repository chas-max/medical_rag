"""Tenant-gated Streamlit console for the local medical RAG project.

The console calls the loopback RAG backend for grounded answers, keeps tenant
accounts/session mappings and per-turn telemetry in a local SQLite database, and
renders multi-version evaluation metrics plus latency/throughput charts. The
login gate is an application-level feature, not backend authentication.
"""

__all__ = ["api_client", "app", "auth", "data", "launcher", "state", "stats", "storage"]
