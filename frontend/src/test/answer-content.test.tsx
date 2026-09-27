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
