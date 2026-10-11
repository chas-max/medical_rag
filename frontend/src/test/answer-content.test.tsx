import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import AnswerContent from "../components/AnswerContent";

afterEach(cleanup);

it("formats verified lines and opens consistently numbered citations", async () => {
  const open = vi.fn();
  const first = { chunk_id: "block-1", filename: "指南.md", excerpt: "原文" };
  const second = { chunk_id: "block-2", filename: "问答.pdf", excerpt: "另一段" };
  render(<AnswerContent content={"要点一 [block-1]\n要点二 [block-2] [block-1] [unknown]"}
    citations={[first, second]} abstained={false} onCitationClick={open} />);
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(screen.queryByText(/block-1/)).not.toBeInTheDocument();
  expect(screen.getByText(/\[unknown\]/)).toBeInTheDocument();
  const firstRefs = screen.getAllByRole("button", { name: "查看引用 1：指南.md" });
  expect(firstRefs).toHaveLength(2);
  await userEvent.click(firstRefs[1]);
  expect(open).toHaveBeenCalledWith(first);
});

it("preserves old text when citation metadata is unavailable", () => {
  render(<AnswerContent content="旧回答 [legacy]" citations={[{ filename: "旧资料" }]} abstained={false}
    onCitationClick={vi.fn()} />);
  expect(screen.getByText("旧回答 [legacy]")).toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

it("preserves original multiline text when citation ids have no matching markers", () => {
  const content = "  旧回答 [legacy]\n\n  下一段 <b>原样</b>  ";
  const { container } = render(<AnswerContent content={content}
    citations={[{ chunk_id: "block-1", filename: "指南.md" }]}
    abstained={false} onCitationClick={vi.fn()} />);
  expect(container.querySelector(".message-text")?.textContent).toBe(content);
  expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  expect(container.querySelector("b")).toBeNull();
});

it("renders the interpretive section as a separate block below the verified claims", () => {
  render(
    <AnswerContent
      content={"要点一 [block-1]\n要点二 [block-2]"}
      citations={[
        { chunk_id: "block-1", filename: "指南.md", excerpt: "要点一" },
        { chunk_id: "block-2", filename: "问答.pdf", excerpt: "要点二" },
      ]}
      analysis={{
        summary: "这两条都指向长期管理，需要按医嘱坚持。",
        extensions: ["复诊时带上既往资料，便于医生对照。"],
        based_on: ["block-1"],
      }}
      abstained={false}
      onCitationClick={vi.fn()}
    />,
  );
  // 结论仍然是两条列表项，解释段不混进结论计数。
  expect(screen.getAllByRole("listitem")).toHaveLength(3);
  expect(screen.getByRole("region", { name: "延伸解读" })).toBeInTheDocument();
  expect(screen.getByText("延伸解读 · 非事实来源")).toBeInTheDocument();
  expect(screen.getByText("这两条都指向长期管理，需要按医嘱坚持。")).toBeInTheDocument();
  expect(screen.getByText("复诊时带上既往资料，便于医生对照。")).toBeInTheDocument();
});

it("never shows the interpretive section on an abstention", () => {
  render(
    <AnswerContent
      content="现有资料不足以回答该问题。"
      citations={[]}
      analysis={{ summary: "这段解释不该出现。", extensions: [], based_on: ["block-1"] }}
      abstained={true}
      onCitationClick={vi.fn()}
    />,
  );
  expect(screen.queryByRole("region", { name: "延伸解读" })).not.toBeInTheDocument();
  expect(screen.queryByText("这段解释不该出现。")).not.toBeInTheDocument();
  expect(screen.getByText("现有资料不足以回答该问题。")).toBeInTheDocument();
});
