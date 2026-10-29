"""Dependency-free lexical matching used by the reranker fallback and the gate.

The fallback must answer one question: how much of the *content* of the query does
this chunk actually cover? A raw character-overlap ratio is too generous for
Chinese because a single shared character is normal in unrelated medical text, so
single-character function words are removed and a minimum hit count is required.
"""

import re

# Function characters that carry no retrieval signal on their own. CJK tokenization
# here is character based (same as the embedder vocabulary assumption), so a
# stoplist of single characters is the smallest change that removes "如何/什么/
# 患者" style noise without pulling in a segmenter dependency.
_STOP_CHARS = frozenset(
    "的了是在和与及或有无如何怎样怎么什么吗呢吧啊呀请问我你他她它这那些个"
    "为对从到用把被就都也很更最可以能会要需应该么样哪谁若按将并但其之此"
)

# Conservative policy for evidence scored lexically instead of by the cross-encoder.
# Two hits keep a single shared character from qualifying; 0.6 coverage means most of
# the query's content words must occur in the chunk.
LEXICAL_MIN_HITS = 2
LEXICAL_MIN_COVERAGE = 0.6

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """Return lower-cased words and CJK characters without external dependencies."""
    return _TOKEN_PATTERN.findall(text.casefold())


def content_tokens(text: str) -> list[str]:
    """Drop single-character function words, keeping alphanumeric terms intact."""
    return [token for token in tokenize(text)
            if not (len(token) == 1 and token in _STOP_CHARS)]


def coverage(question: str, text: str) -> tuple[int, float]:
    """Return (content-word hits, share of the query's content words covered)."""
    query_tokens = set(content_tokens(question))
    if not query_tokens:
        return 0, 0.0
    hits = len(query_tokens & set(content_tokens(text)))
    return hits, hits / len(query_tokens)


def score(question: str, text: str) -> float:
    """Lexical relevance in [0, 1], or 0 when the conservative hit floor is missed."""
    hits, ratio = coverage(question, text)
    if hits < LEXICAL_MIN_HITS:
        return 0.0
    return ratio
