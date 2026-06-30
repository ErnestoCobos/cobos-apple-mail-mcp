"""Text utilities shared by the indexer, search, and threader: HTML -> plain
text extraction, subject normalization (for threading fallback), and FTS5
query sanitization (so user queries can never break MATCH syntax).
"""

from __future__ import annotations

import re

_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(re|fw|fwd|aw|wg|sv|tr|antwort|odp|res)\s*(\[\d+\])?\s*:\s*",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")
_FTS_SPECIAL_RE = re.compile(r'["*^]')


def html_to_text(html: str) -> str:
    """Extract readable plain text from an HTML email body."""
    if not html:
        return ""
    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        for tag in tree.css("script, style, head"):
            tag.decompose()
        text = tree.body.text(separator="\n", strip=True) if tree.body else tree.text(strip=True)
        return _WS_RE.sub(" ", text).strip()
    except Exception:
        # Fall back to a crude tag stripper rather than failing indexing.
        stripped = re.sub(r"<[^>]+>", " ", html)
        return _WS_RE.sub(" ", stripped).strip()


def normalize_subject(subject: str | None) -> str:
    """Strip Re:/Fwd:/list-tag prefixes (repeatedly) for threading fallback
    and for awaiting-reply / duplicate-subject heuristics.
    """
    if not subject:
        return ""
    value = subject.strip()
    while True:
        new_value = _SUBJECT_PREFIX_RE.sub("", value)
        if new_value == value:
            break
        value = new_value
    return _WS_RE.sub(" ", value).strip().lower()


def sanitize_fts_query(query: str) -> str:
    """Make a user-supplied query safe to embed in an FTS5 MATCH expression.

    Strategy: if the query already looks like it's using FTS5 operators
    (quoted phrases, AND/OR/NOT, prefix `*`), pass it through mostly as-is
    but balance quotes. Otherwise, treat each whitespace-separated token as
    a literal AND-ed term, quoting any token containing FTS5-special
    characters so it can't be interpreted as syntax.
    """
    query = query.strip()
    if not query:
        return '""'

    if query.count('"') % 2 != 0:
        # Unbalanced quote -> escape all quotes and treat literally.
        query = query.replace('"', '""')
        return f'"{query}"'

    tokens = query.split()
    upper_ops = {"AND", "OR", "NOT", "NEAR"}
    safe_tokens: list[str] = []
    for token in tokens:
        if token.upper() in upper_ops or token.startswith('"') or token.endswith('"'):
            safe_tokens.append(token)
            continue
        if _FTS_SPECIAL_RE.search(token.rstrip("*")) or ":" in token:
            escaped = token.replace('"', '""')
            safe_tokens.append(f'"{escaped}"')
        else:
            safe_tokens.append(token)
    return " ".join(safe_tokens)


_NOREPLY_RE = re.compile(
    r"\b(no[-._]?reply|do[-._]?not[-._]?reply|notifications?|mailer-daemon|"
    r"postmaster|automated)\b",
    re.IGNORECASE,
)


def looks_like_noreply(address: str | None) -> bool:
    if not address:
        return False
    return bool(_NOREPLY_RE.search(address))
