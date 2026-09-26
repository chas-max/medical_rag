import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import ChatPage from "./pages/ChatPage";
import WorkflowEditorPage from "./pages/WorkflowEditorPage";

export default function App() {
  const location = useLocation();
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
            to="/workflows/medical-answer"
          >
            ◇ &nbsp; 工作流编辑器
          </Link>
        </nav>
        <span className="topbar-end">
          公开资料检索工作台 <span className="status-dot" />
        </span>
      </header>
      <Routes>
        <Route path="/chat" element={<ChatPage />} />
        <Route
          path="/workflows"
          element={<Navigate to="/workflows/medical-answer" replace />}
        />
        <Route path="/workflows/:workflowId" element={<WorkflowEditorPage />} />
        <Route path="*" element={<Navigate to="/chat" replace />} />
      </Routes>
    </div>
  );
}
