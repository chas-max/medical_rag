import { Fragment } from "react";
import type { Citation } from "../api/workflows";

type Props = {
  content: string;
  citations: Citation[];
  abstained: boolean;
  onCitationClick: (citation: Citation) => void;
};

export default function AnswerContent({ content, citations, abstained, onCitationClick }: Props) {
  if (abstained || !citations.length) return <div className="message-text">{content}</div>;
  const sourceById = new Map<string, { citation: Citation; number: number }>();
  citations.forEach((citation) => {
    if (citation.chunk_id && !sourceById.has(citation.chunk_id)) {
      sourceById.set(citation.chunk_id, { citation, number: sourceById.size + 1 });
    }
  });
  if (!sourceById.size || !Array.from(content.matchAll(/\[([^[\]\n]+)\]/g))
    .some((match) => sourceById.has(match[1]))) {
    return <div className="message-text">{content}</div>;
  }
  const lines = content.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const renderLine = (line: string) => line.split(/(\[[^[\]\n]+\])/g).map((part, index) => {
    const match = /^\[([^[\]\n]+)\]$/.exec(part);
    const source = match ? sourceById.get(match[1]) : undefined;
    return source ? (
      <button key={index} type="button" className="citation-ref"
        aria-label={`查看引用 ${source.number}：${source.citation.filename || "资料"}`}
        onClick={() => onCitationClick(source.citation)}>[{source.number}]</button>
    ) : <Fragment key={index}>{part}</Fragment>;
  });
  if (lines.length <= 1) return <div className="message-text">{renderLine(lines[0] || content)}</div>;
  return <ol className="answer-list">{lines.map((line, index) =>
    <li key={index}>{renderLine(line)}</li>)}</ol>;
}
