"""Query wrapper cleanup, clinical acronym expansion, and guideline title helpers."""

from __future__ import annotations

import re
from pathlib import Path

_WRAPPER_SENTENCES = (
    re.compile(
        r"(?:现有|当前|上述)(?:医学)?(?:指南|资料|证据|文献|食养指南|知识库)"
        r"(?:[与和或][^，。；？?]{0,16})?(?:能否|是否能?)(?:提供|满足|覆盖|解答)[^？?。]*[？?。]?"
    ),
    re.compile(r"请?(?:根据|结合|查阅|对照|依据|参照)[^，,:：\n]{1,18}(?:阐述|分析|解答|对比|说明|回答)[，,:：]?"),
)
_BOOK_TITLE = re.compile(r"《([^》]{2,60})》")
_NON_ALNUM = re.compile(r"[^\u4e00-\u9fffa-z0-9]+")

CLINICAL_SYNONYMS: dict[str, tuple[str, ...]] = {
    "ckd": ("慢性肾脏病",),
    "glp-1ra": ("胰高糖素样肽-1受体激动剂",),
    "sglt2i": ("钠-葡萄糖协同转运蛋白2抑制剂", "SGLT2抑制剂"),
    "sglt2抑制剂": ("钠-葡萄糖协同转运蛋白2抑制剂", "达格列净"),
    "eos": ("嗜酸性粒细胞",),
    "ogtt": ("口服葡萄糖耐量试验",),
    "egfr": ("估算肾小球滤过率",),
    "hfpef": ("射血分数保留型心力衰竭",),
    "hfref": ("射血分数降低型心力衰竭",),
    "aecopd": ("慢性阻塞性肺疾病急性加重",),
    "cnlc": ("中国肝癌分期",),
    "ppg": ("光电容积脉搏波",),
    "ptt": ("脉搏传导时间",),
    "arb": ("血管紧张素Ⅱ受体拮抗剂",),
    "acei": ("血管紧张素转换酶抑制剂",),
    "ccb": ("钙通道阻滞剂",),
    "nsclc": ("非小细胞肺癌",),
    "mpp": ("肺炎支原体肺炎",),
    "sua": ("血尿酸",),
    "hba1c": ("糖化血红蛋白",),
    "inr": ("国际标准化比值",),
    "bmi": ("体质指数",),
    "rygb": ("Roux-en-Y胃旁路术",),
    "sg": ("胃袖状切除术",),
}


def strip_query_wrappers(question: str) -> str:
    """Remove meta-question framing while keeping every clinical entity and 《title》."""
    text = (question or "").strip()
    if not text:
        return ""
    cleaned = text
    for pattern in _WRAPPER_SENTENCES:
        cleaned = pattern.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，,。；;：:")
    return cleaned if len(cleaned) >= 8 else text


def expand_clinical_synonyms(queries: list[str], *, max_extra: int = 2) -> list[str]:
    """Append concise synonym-expanded variants for clinical acronyms without flooding."""
    if not queries or max_extra < 1:
        return list(queries)
    extra: list[str] = []
    seen = {q.strip() for q in queries if q.strip()}
    for query in queries:
        lower = query.lower()
        additions: list[str] = []
        for term, aliases in CLINICAL_SYNONYMS.items():
            if len(term) <= 4 and term.isascii():
                right_boundary = r"(?![a-z0-9])" if len(term) <= 2 else r"(?![a-z])"
                matched = bool(re.search(rf"(?<![a-z0-9]){re.escape(term)}{right_boundary}", lower))
            else:
                matched = term in lower
            if not matched:
                continue
            for alias in aliases:
                if alias.lower() not in lower and alias not in additions:
                    additions.append(alias)
                    break
        if additions:
            variant = f"{query} {' '.join(additions[:2])}".strip()
            if variant not in seen:
                seen.add(variant)
                extra.append(variant)
                if len(extra) >= max_extra:
                    break
    return [*queries, *extra]


def normalize_title_key(title: str) -> str:
    """Canonical key for comparing guideline filenames against 《title》 mentions."""
    stem = Path(title).stem if "." in title else title
    return _NON_ALNUM.sub("", stem.lower())


def extract_guideline_titles(text: str) -> list[str]:
    """Return unique 《title》 strings referenced in a question."""
    return list(dict.fromkeys(match.strip() for match in _BOOK_TITLE.findall(text or "") if match.strip()))


def extract_query_terms(text: str) -> list[str]:
    """Extract normalized 2+ char alphanumeric/CJK terms for gap-coverage checks."""
    tokens = [tok.lower() for tok in re.findall(r"[a-z0-9\-]+|[\u4e00-\u9fff]{2,3}", text or "")]
    return [tok for tok in dict.fromkeys(tokens) if len(tok) >= 2]


def filename_matches_title(filename: str, titles: list[str]) -> bool:
    """Check whether a source filename matches any explicit 《title》 mention."""
    if not filename or not titles:
        return False
    file_key = normalize_title_key(filename)
    if not file_key:
        return False
    for title in titles:
        title_key = normalize_title_key(title)
        if title_key and (title_key in file_key or file_key in title_key):
            return True
    return False
