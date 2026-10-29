import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import MemorySection from "../settings/MemorySection";
import * as memory from "../api/memory";

vi.mock("../api/memory", () => ({
  getMemory: vi.fn(),
  setMemoryEnabled: vi.fn(),
  saveMemoryProfile: vi.fn(),
  deleteMemoryProfile: vi.fn(),
  getMemoryEpisode: vi.fn(),
  deleteMemoryEpisode: vi.fn(),
  clearMemory: vi.fn(),
  getMemoryJob: vi.fn(),
}));

const state: memory.MemoryState = {
  enabled: true,
  revision: 1,
  profiles: [
    {
      key: "response_style",
      value: "简短中文",
      category: "preferences",
      sensitive: false,
      source: "manual",
      support: "",
      source_run_id: null,
    },
  ],
  episodes: [
    {
      id: "episode-1",
      title: "随访讨论",
      created_at: "2026-10-18T00:00:00Z",
      source_run_id: "run-1",
      sensitive: false,
    },
  ],
  jobs: [],
};

beforeEach(() => {
  vi.mocked(memory.getMemory).mockResolvedValue(state);
});
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

it("discloses local shared memory costs and toggles without deleting existing records", async () => {
  vi.mocked(memory.setMemoryEnabled).mockResolvedValue({
    ...state,
    enabled: false,
  });
  const user = userEvent.setup();
  render(<MemorySection />);
  expect(
    await screen.findByText(
      "意图分析和情景提取使用当前模型，会增加模型费用和延迟；纯画像管理不检索资料，也不产生情景记忆。",
    ),
  ).toBeInTheDocument();
  expect(screen.getByText(/所有浏览器共享同一个本地用户/)).toBeInTheDocument();
  expect(screen.getByText(/不会删除聊天历史/)).toBeInTheDocument();
  await user.click(
    await screen.findByRole("checkbox", { name: "启用长期记忆" }),
  );
  await waitFor(() =>
    expect(memory.setMemoryEnabled).toHaveBeenCalledWith(false),
  );
  expect(screen.getByText("简短中文")).toBeInTheDocument();
  expect(
    screen.getByRole("checkbox", { name: "启用长期记忆" }),
  ).not.toBeChecked();
});

it("adds and corrects profile entries with explicit manual sensitive consent", async () => {
  const user = userEvent.setup();
  vi.mocked(memory.saveMemoryProfile).mockResolvedValue({
    ...state,
    profiles: [{ ...state.profiles[0], value: "分点中文" }],
  });
  render(<MemorySection />);
  await user.click(await screen.findByRole("button", { name: "编辑回答偏好" }));
  const input = screen.getByLabelText("画像内容");
  expect(input).toHaveValue("简短中文");
  await user.clear(input);
  await user.type(input, "分点中文");
  await user.click(screen.getByRole("button", { name: "保存画像" }));
  await waitFor(() =>
    expect(memory.saveMemoryProfile).toHaveBeenCalledWith("response_style", {
      value: "分点中文",
      category: "preferences",
      sensitive: false,
    }),
  );
  expect(await screen.findByText("分点中文")).toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText("画像字段"), "health");
  await user.type(screen.getByLabelText("画像内容"), "用户自述");
  expect(screen.getByRole("button", { name: "保存画像" })).toBeDisabled();
  await user.click(screen.getByLabelText("我同意保存此条敏感信息"));
  await user.click(screen.getByRole("button", { name: "保存画像" }));
  await waitFor(() =>
    expect(memory.saveMemoryProfile).toHaveBeenLastCalledWith("health", {
      value: "用户自述",
      category: "health",
      sensitive: true,
    }),
  );
});

it("requires confirmation for individual deletion and clearing all memory", async () => {
  const user = userEvent.setup();
  vi.mocked(memory.deleteMemoryProfile).mockResolvedValue({
    ...state,
    profiles: [],
  });
  vi.mocked(memory.clearMemory).mockResolvedValue({
    ...state,
    profiles: [],
    episodes: [],
  });
  render(<MemorySection />);
  await user.click(await screen.findByRole("button", { name: "删除回答偏好" }));
  expect(memory.deleteMemoryProfile).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "确认删除回答偏好" }));
  await waitFor(() =>
    expect(memory.deleteMemoryProfile).toHaveBeenCalledWith("response_style"),
  );
  await user.click(screen.getByRole("button", { name: "清空全部记忆" }));
  expect(memory.clearMemory).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "确认清空全部记忆" }));
  await waitFor(() => expect(memory.clearMemory).toHaveBeenCalledOnce());
  expect(await screen.findByText("暂无情景记忆")).toBeInTheDocument();
});

it("reads Markdown as plain text and confirms episode removal", async () => {
  const user = userEvent.setup();
  vi.mocked(memory.getMemoryEpisode).mockResolvedValue({
    id: "episode-1",
    content: "# 情景\n<script>alert(1)</script>",
  });
  vi.mocked(memory.deleteMemoryEpisode).mockResolvedValue({
    ...state,
    episodes: [],
  });
  render(<MemorySection />);
  await user.click(await screen.findByRole("button", { name: "阅读随访讨论" }));
  expect(
    await screen.findByText(/<script>alert\(1\)<\/script>/),
  ).toBeInTheDocument();
  expect(document.querySelector("script")).toBeNull();
  await user.click(screen.getByRole("button", { name: "删除情景随访讨论" }));
  expect(memory.deleteMemoryEpisode).not.toHaveBeenCalled();
  await user.click(
    screen.getByRole("button", { name: "确认删除情景随访讨论" }),
  );
  expect(await screen.findByText("暂无情景记忆")).toBeInTheDocument();
  expect(screen.queryByText(/<script>/)).not.toBeInTheDocument();
});

it("reports server errors without pretending memory was changed", async () => {
  const user = userEvent.setup();
  vi.mocked(memory.setMemoryEnabled).mockRejectedValue(
    new Error("数据库不可写"),
  );
  render(<MemorySection />);
  await user.click(
    await screen.findByRole("checkbox", { name: "启用长期记忆" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("数据库不可写");
  expect(screen.getByRole("checkbox", { name: "启用长期记忆" })).toBeChecked();
});

it("reports initial load errors and allows retry", async () => {
  const user = userEvent.setup();
  vi.mocked(memory.getMemory).mockRejectedValueOnce(new Error("暂不可用"));
  render(<MemorySection />);
  expect(await screen.findByRole("alert")).toHaveTextContent("暂不可用");
  await user.click(screen.getByRole("button", { name: "重试读取记忆" }));
  expect(await screen.findByText("简短中文")).toBeInTheDocument();
});

it("preserves unsaved profile content when saving fails", async () => {
  const user = userEvent.setup();
  vi.mocked(memory.saveMemoryProfile).mockRejectedValue(
    new Error("画像保存失败"),
  );
  render(<MemorySection />);
  const input = await screen.findByLabelText("画像内容");
  await user.type(input, "小明");
  await user.click(screen.getByRole("button", { name: "保存画像" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("画像保存失败");
  expect(input).toHaveValue("小明");
});
