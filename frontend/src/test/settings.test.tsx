import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "../App";
import SettingsDialog from "../components/SettingsDialog";
import * as api from "../api/workflows";
import * as settings from "../api/settings";

vi.mock("../api/workflows", () => ({
  getNodes: vi.fn(),
  getWorkflows: vi.fn(),
  getWorkflow: vi.fn(),
  getHealth: vi.fn(),
  runWorkflow: vi.fn(),
  streamWorkflow: vi.fn(),
  validateWorkflow: vi.fn(),
  saveWorkflow: vi.fn(),
  uploadDocument: vi.fn(),
}));

vi.mock("../api/settings", () => ({
  getModelConfig: vi.fn(),
  saveModelConfig: vi.fn(),
  selectModelProfile: vi.fn(),
  deleteModelProfile: vi.fn(),
}));

const DASHSCOPE = "https://dashscope.aliyuncs.com/compatible-mode/v1";
const profiles: settings.ModelProfile[] = [
  {
    id: "qwen3-8-flash",
    model_name: "qwen3.8-flash",
    base_url: DASHSCOPE,
    api_key_masked: "••••1234",
    verified_at: new Date().toISOString(),
    active: true,
  },
  {
    id: "deepseek-chat",
    model_name: "deepseek-chat",
    base_url: "https://api.deepseek.com/v1",
    api_key_masked: "••••5678",
    verified_at: null,
    active: false,
  },
];

const stored: settings.ModelConfig = {
  model_name: "qwen3.8-flash",
  base_url: DASHSCOPE,
  api_key_masked: "••••1234",
  configured: true,
  source: "settings",
  verified_at: profiles[0].verified_at,
  active_id: "qwen3-8-flash",
  profiles,
  fallback: { model_name: "qwen3.8-flash", base_url: DASHSCOPE, configured: false },
};

function switched(): settings.ModelConfig {
  return {
    ...stored,
    model_name: "deepseek-chat",
    base_url: "https://api.deepseek.com/v1",
    api_key_masked: "••••5678",
    active_id: "deepseek-chat",
    verified_at: null,
    source: "settings",
    profiles: [
      { ...profiles[0], active: false },
      { ...profiles[1], active: true },
    ],
  };
}

beforeEach(() => {
  localStorage.clear();
  vi.mocked(settings.getModelConfig).mockResolvedValue(stored);
  vi.mocked(api.getWorkflows).mockResolvedValue([]);
  vi.mocked(api.getNodes).mockResolvedValue([]);
  vi.mocked(api.getHealth).mockResolvedValue({ milvus: true });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const open = () => render(<SettingsDialog open onClose={vi.fn()} />);

it("lists every stored model and keeps the active one marked", async () => {
  open();
  const running = (await screen.findByText("qwen3.8-flash")).closest(".profile-row");
  expect(running).toHaveClass("active");
  expect(running).toHaveTextContent("••••1234");
  expect(running).toHaveTextContent("刚刚校验");
  expect(screen.getByText("使用中")).toBeInTheDocument();
  const other = screen.getByText("deepseek-chat").closest(".profile-row");
  expect(other).toHaveTextContent("••••5678");
  expect(other).not.toHaveTextContent("刚刚校验");
  expect(screen.getByRole("button", { name: "切换" })).toBeInTheDocument();
  expect(screen.getByLabelText("模型名称")).toHaveValue("qwen3.8-flash");
  expect(screen.getByLabelText("API Key")).toHaveValue("");
});

it("activates a new model through the confirm button", async () => {
  const user = userEvent.setup();
  vi.mocked(settings.saveModelConfig).mockResolvedValue({
    ...stored,
    model_name: "qwen-max",
    active_id: "qwen-max",
  });
  open();
  const name = await screen.findByLabelText("模型名称");
  await user.clear(name);
  await user.type(name, " qwen-max ");
  await user.type(screen.getByLabelText("API Key"), " sk-new-key-9999 ");
  await user.click(screen.getByRole("button", { name: "确认并生效" }));
  await waitFor(() =>
    expect(settings.saveModelConfig).toHaveBeenCalledWith({
      model_name: "qwen-max",
      base_url: DASHSCOPE,
      api_key: "sk-new-key-9999",
    }),
  );
  expect(await screen.findByRole("status")).toHaveTextContent("已保存并切换到 qwen-max");
  expect(screen.getByLabelText("API Key")).toHaveValue("");
  expect(screen.queryByText(/sk-new-key-9999/)).not.toBeInTheDocument();
});

it("switches to a stored model without probing it again", async () => {
  const user = userEvent.setup();
  vi.mocked(settings.selectModelProfile).mockResolvedValue(switched());
  open();
  await user.click(await screen.findByRole("button", { name: "切换" }));
  await waitFor(() => expect(settings.selectModelProfile).toHaveBeenCalledWith("deepseek-chat"));
  expect(settings.saveModelConfig).not.toHaveBeenCalled();
  expect(await screen.findByRole("status")).toHaveTextContent(
    "已切换到 deepseek-chat，未再发起校验",
  );
  expect(screen.getByText("当前生效")).toBeInTheDocument();
  expect(screen.getByLabelText("模型名称")).toHaveValue("deepseek-chat");
});

it("asks twice before deleting a stored key", async () => {
  const user = userEvent.setup();
  vi.mocked(settings.deleteModelProfile).mockResolvedValue({
    ...stored,
    profiles: [profiles[0]],
  });
  open();
  await screen.findByText("deepseek-chat");
  const remove = screen.getAllByRole("button", { name: "移除" });
  expect(remove).toHaveLength(2);
  await user.click(remove[1]);
  expect(settings.deleteModelProfile).not.toHaveBeenCalled();
  expect(await screen.findByRole("alert")).toHaveTextContent("已存密钥");
  const armed = await screen.findByRole("button", { name: "确认移除" });
  await user.click(armed);
  await waitFor(() => expect(settings.deleteModelProfile).toHaveBeenCalledWith("deepseek-chat"));
  expect(await screen.findByRole("status")).toHaveTextContent("已移除 deepseek-chat");
});

it("refuses to probe until every field is filled", async () => {
  const user = userEvent.setup();
  open();
  await user.type(await screen.findByLabelText("API Key"), "x");
  await user.clear(screen.getByLabelText("模型名称"));
  await user.click(screen.getByRole("button", { name: "确认并生效" }));
  expect(settings.saveModelConfig).not.toHaveBeenCalled();
  expect(await screen.findByRole("alert")).toHaveTextContent("都需要填写");
});

it("keeps the current model when the endpoint rejects the new one", async () => {
  const user = userEvent.setup();
  vi.mocked(settings.saveModelConfig).mockRejectedValue(
    new Error("API Key 无效，或该 Key 没有此模型的访问权限，请重新填写"),
  );
  const onClose = vi.fn();
  render(<SettingsDialog open onClose={onClose} />);
  await user.type(await screen.findByLabelText("API Key"), "sk-wrong");
  await user.click(screen.getByRole("button", { name: "确认并生效" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "API Key 无效，或该 Key 没有此模型的访问权限，请重新填写（当前模型未变）",
  );
  expect(screen.getByRole("dialog", { name: "设置" })).toBeInTheDocument();
  expect(onClose).not.toHaveBeenCalled();
});

it("reaches the settings dialog from the gear in the top bar", async () => {
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={["/chat"]}>
      <App />
    </MemoryRouter>,
  );
  expect(
    await screen.findByRole("heading", { name: /从可信资料中寻找答案/ }),
  ).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "设置" }));
  expect(await screen.findByRole("dialog", { name: "设置" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "关闭设置" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
