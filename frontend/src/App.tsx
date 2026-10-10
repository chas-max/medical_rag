import { useState, useSyncExternalStore } from "react";
import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import ChatPage from "./pages/ChatPage";
import SettingsDialog from "./components/SettingsDialog";
import WorkflowEditorPage from "./pages/WorkflowEditorPage";
import {
  readCurrentWorkflowId,
  subscribeCurrentWorkflowId,
} from "./workflow/selection";

function GearIcon() {
  return (
    <svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true" focusable="false">
      <path
        fill="currentColor"
        d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 0 0 .12-.61l-1.92-3.32a.48.48 0 0 0-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.48.48 0 0 0-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.48.48 0 0 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 0 0-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6a3.6 3.6 0 1 1 0-7.2 3.6 3.6 0 0 1 0 7.2z"
      />
    </svg>
  );
}

export default function App() {
  const location = useLocation();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const currentWorkflow =
    useSyncExternalStore(subscribeCurrentWorkflowId, readCurrentWorkflowId) ||
    "medical-answer";
  return (
    <div className="app-shell">
      <header className="topbar">
        <Link className="brand" to="/chat">
          <span className="brand-icon">✳</span>
          <span>
            知源 <small>MEDICAL RAG</small>
          </span>
        </Link>
        <nav aria-label="主导航" className="topnav">
          <Link
            className={location.pathname.startsWith("/chat") ? "active" : ""}
            to="/chat"
          >
            ◎ &nbsp; 医疗问答
          </Link>
          <Link
            className={
              location.pathname.startsWith("/workflows") ? "active" : ""
            }
            to={`/workflows/${currentWorkflow}`}
          >
            ◇ &nbsp; 工作流编辑器
          </Link>
        </nav>
        <span className="topbar-tail">
          <span className="topbar-end">
            公开资料检索工作台 <span className="status-dot" />
          </span>
          <button
            type="button"
            className="settings-trigger"
            onClick={() => setSettingsOpen(true)}
            aria-label="设置"
            aria-haspopup="dialog"
            title="设置"
          >
            <GearIcon />
          </button>
        </span>
      </header>
      <Routes>
        <Route path="/chat" element={<ChatPage />} />
        <Route
          path="/workflows"
          element={<Navigate to={`/workflows/${currentWorkflow}`} replace />}
        />
        <Route path="/workflows/:workflowId" element={<WorkflowEditorPage />} />
        <Route path="*" element={<Navigate to="/chat" replace />} />
      </Routes>
      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </div>
  );
}
