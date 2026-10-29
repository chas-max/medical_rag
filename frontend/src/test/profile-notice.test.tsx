import { cleanup, render, screen, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { ProfileNotice } from "../chat/ProfileNotice";
import type { ProfileOutcome } from "../api/workflows";
import { ApiRequestError } from "../api/client";
import { confirmProfile } from "../api/chat";
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
const outcome: ProfileOutcome = {
  status: "processed",
  confirmation_token: "bound-token-value-123",
  confirmation_expires_at: Date.now() / 1000 + 300,
  confirmation_requires_consent: false,
  actions: [
    {
      key: "occupation",
      status: "needs_confirmation",
      message: "确认修改职业画像",
    },
  ],
};
it("requires an explicit confirmation click", async () => {
  const onConfirm = vi.fn().mockResolvedValue(undefined);
  render(
    <ProfileNotice disabled={false} onConfirm={onConfirm} outcome={outcome} />,
  );
  expect(screen.getByRole("status")).toHaveTextContent("确认修改职业画像");
  expect(onConfirm).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "确认画像变更" }));
  expect(onConfirm).toHaveBeenCalledWith(true);
});
it("never offers to confirm sensitive changes and always permits cancellation", async () => {
  const onConfirm = vi.fn().mockResolvedValue(undefined);
  render(
    <ProfileNotice
      disabled={false}
      onConfirm={onConfirm}
      outcome={{ ...outcome, confirmation_requires_consent: true }}
    />,
  );
  expect(
    screen.queryByRole("button", { name: "确认画像变更" }),
  ).not.toBeInTheDocument();
  expect(
    screen.getByText("此变更包含敏感信息，默认不保存；如需记录请到设置页「长期记忆」手动添加。"),
  ).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "取消画像变更" }));
  expect(onConfirm).toHaveBeenCalledWith(false);
});
it("prevents double clicks while awaiting the server", async () => {
  let resolve!: () => void;
  const onConfirm = vi.fn().mockImplementation(
    () =>
      new Promise<void>((done) => {
        resolve = done;
      }),
  );
  render(
    <ProfileNotice
      disabled={false}
      onConfirm={onConfirm}
      outcome={{ ...outcome, confirmation_requires_consent: true }}
    />,
  );
  await userEvent.click(screen.getByRole("button", { name: "取消画像变更" }));
  expect(onConfirm).toHaveBeenCalledWith(false);
  expect(screen.getByRole("button", { name: "取消画像变更" })).toBeDisabled();
  await act(async () => resolve());
});
it("hides expired confirmations and expires an open confirmation", async () => {
  vi.useFakeTimers();
  render(
    <ProfileNotice
      disabled={false}
      onConfirm={vi.fn()}
      outcome={{ ...outcome, confirmation_expires_at: Date.now() / 1000 + 1 }}
    />,
  );
  expect(
    screen.getByRole("button", { name: "确认画像变更" }),
  ).toBeInTheDocument();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1001);
  });
  expect(
    screen.queryByRole("button", { name: "确认画像变更" }),
  ).not.toBeInTheDocument();
  expect(screen.getByText(/确认已到期/)).toBeInTheDocument();
});

it("shows safe status messages without treating them as markup or exposing tokens", () => {
  render(
    <ProfileNotice
      disabled={false}
      onConfirm={vi.fn()}
      outcome={{
        status: "conflict",
        actions: [
          {
            key: "health",
            status: "conflict",
            message: "<script>unsafe</script>版本冲突",
          },
        ],
      }}
    />,
  );
  expect(screen.getByRole("status")).toHaveTextContent(
    "<script>unsafe</script>版本冲突",
  );
  expect(document.querySelector("script")).toBeNull();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});
it.each([
  ["disabled", "画像记忆已关闭"],
  ["failed", "画像处理失败；回答状态单独显示"],
] as const)("shows an empty %s outcome honestly", (status, message) => {
  render(
    <ProfileNotice
      disabled={false}
      onConfirm={vi.fn()}
      outcome={{ status, actions: [] }}
    />,
  );
  expect(screen.getByRole("status")).toHaveTextContent(message);
});
it("replaces the sensitive notice when a different confirmation arrives", async () => {
  const props = {
    disabled: false,
    onConfirm: vi.fn(),
    outcome: { ...outcome, confirmation_requires_consent: true },
  };
  const view = render(<ProfileNotice {...props} />);
  expect(
    screen.getByText("此变更包含敏感信息，默认不保存；如需记录请到设置页「长期记忆」手动添加。"),
  ).toBeInTheDocument();
  view.rerender(
    <ProfileNotice
      {...props}
      outcome={{ ...props.outcome, confirmation_token: "another-bound-token",
                 confirmation_requires_consent: false }}
    />,
  );
  expect(
    screen.queryByText("此变更包含敏感信息，默认不保存；如需记录请到设置页「长期记忆」手动添加。"),
  ).not.toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "确认画像变更" }),
  ).toBeEnabled();
});

it.each([
  "MEMORY_CONFIRMATION_EXPIRED",
  "MEMORY_CONFIRMATION_CONFLICT",
  "MEMORY_CONFIRMATION_INVALID",
])(
  "ends the confirmation on rejected %s instead of offering endless network retries",
  async (code) => {
    const onConfirm = vi
      .fn()
      .mockRejectedValue(
        new ApiRequestError("确认已失效，请重新提出变更", code, 409),
      );
    render(
      <ProfileNotice
        disabled={false}
        onConfirm={onConfirm}
        outcome={outcome}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "确认画像变更" }));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "确认已失效，请重新提出变更",
    );
    expect(
      screen.queryByRole("button", { name: "确认画像变更" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "取消画像变更" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/检查连接后重试/)).not.toBeInTheDocument();
  },
);
it("keeps unrelated request conflicts retryable and does not expose unclassified error details", async () => {
  const onConfirm = vi
    .fn()
    .mockRejectedValue(
      new ApiRequestError(
        "private conflict details",
        "REQUEST_ID_CONFLICT",
        409,
      ),
    );
  render(
    <ProfileNotice disabled={false} onConfirm={onConfirm} outcome={outcome} />,
  );
  await userEvent.click(screen.getByRole("button", { name: "确认画像变更" }));
  expect(screen.getByRole("alert")).toHaveTextContent(
    "画像确认失败，请检查连接后重试",
  );
  expect(screen.getByRole("button", { name: "确认画像变更" })).toBeEnabled();
  expect(
    screen.queryByText(/private conflict details/),
  ).not.toBeInTheDocument();
});

it("handles the actual HTTP409 confirmation envelope through the real client", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        success: false,
        data: null,
        error: {
          code: "MEMORY_CONFIRMATION_CONFLICT",
          message: "画像已变更，请重新发起确认",
        },
      }),
      { status: 409 },
    ),
  );
  vi.stubGlobal("fetch", fetchMock);
  render(
    <ProfileNotice
      disabled={false}
      outcome={outcome}
      onConfirm={(accept) =>
        confirmProfile({
          request_id: "confirm-real-client",
          conversation_id: "c1",
          token: outcome.confirmation_token!,
          accept,
        }).then(() => undefined)
      }
    />,
  );
  await userEvent.click(screen.getByRole("button", { name: "确认画像变更" }));
  expect(screen.getByRole("alert")).toHaveTextContent(
    "画像已变更，请重新发起确认",
  );
  expect(
    screen.queryByRole("button", { name: "确认画像变更" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "取消画像变更" }),
  ).not.toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledOnce();
  expect(fetchMock.mock.calls[0][0]).toBe("/api/chat/confirmations");
});
