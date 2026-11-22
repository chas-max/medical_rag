"""Persistent, read-only Streamlit viewer for local demonstration artifacts.

The viewer never writes to the chat database. It persists only its own UI state
(page, last selection, notes) so a restarted process reopens where the operator
left off; see :mod:`medical_rag.ui.state`.
"""

__all__ = ["app", "data", "launcher", "state"]
