import { Fragment } from "react";
import type { AnalysisPayload, Citation } from "../api/workflows";

type Props = {
  content: string;
  citations: Citation[];
  analysis?: AnalysisPayload;
  abstained: boolean;
  onCitationClick: (citation: Citation) => void;
};

export default function AnswerContent({
  content,
  citations,
  analysis,
  abstained,
  onCitationClick,
}: Props) {
  // 解释段没有出处，只能在已有已核对结论之上展示；拒答时整段不出现。
  const section = !abstained && analysis && analysis.summary ? analysis : null;
  if (abstained || !citations.length) {
    return (
      <div className="message-text">
        {content}
        {section && <AnalysisSection analysis={section} />}
      </div>
    );
  }
  const sourceById = new Map<string, { citation: Citation; number: number }>();
  citations.forEach((citation) => {
    if (citation.chunk_id && !sourceById.has(citation.chunk_id)) {
      sourceById.set(citation.chunk_id, { citation, number: sourceById.size + 1 });
    }
  });
  const markClaims = sourceById.size > 0 && Array.from(content.matchAll(/\[([^[\]\n]+)\]/g))
    .some((match) => sourceById.has(match[1]));
  if (!markClaims) {
    return (
      <div className="message-text">
        {content}
        {section && <AnalysisSection analysis={section} />}
      </div>
    );
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
  const claims = lines.length <= 1
    ? <div className="message-text">{renderLine(lines[0] || content)}</div>
    : <ol className="answer-list">{lines.map((line, index) =>
        <li key={index}>{renderLine(line)}</li>)}</ol>;
  if (!section) return claims;
  return (
    <>
      {claims}
      <AnalysisSection analysis={section} />
    </>
  );
}

/** 第二段：对已核对结论的解释与延伸，视觉上与结论区分开，并标明不是事实来源。 */
function AnalysisSection({ analysis }: { analysis: AnalysisPayload }) {
  return (
    <section className="analysis" aria-label="延伸解读">
      <span className="small-label">延伸解读 · 非事实来源</span>
      <p className="analysis-summary">{analysis.summary}</p>
      {!!analysis.extensions?.length && (
        <ul className="analysis-list">
          {analysis.extensions.map((item, index) => <li key={index}>{item}</li>)}
        </ul>
      )}
    </section>
  );
}
