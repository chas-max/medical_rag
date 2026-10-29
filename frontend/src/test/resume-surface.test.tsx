import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import App from "../App";

vi.mock("../pages/ChatPage", () => ({ default: () => <div>问答页面</div> }));
vi.mock("../components/SettingsDialog", () => ({ default: () => null }));

it("only exposes the medical chat, including on an old editor URL", () => {
  render(<MemoryRouter initialEntries={["/workflows/medical-answer"]}><App /></MemoryRouter>);
  expect(screen.getByText("问答页面")).toBeInTheDocument();
  expect(screen.queryByText(/工作流编辑器/)).not.toBeInTheDocument();
});
