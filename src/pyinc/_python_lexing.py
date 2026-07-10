from __future__ import annotations

import io
import tokenize

_IDENTIFIER_START_TOKEN_TYPES = frozenset({tokenize.NAME, tokenize.ERRORTOKEN})
_IDENTIFIER_PART_TOKEN_TYPES = frozenset({tokenize.NAME, tokenize.NUMBER, tokenize.ERRORTOKEN})


def identifier_tokens(source: str) -> tuple[tokenize.TokenInfo, ...]:
    """Return source-spelled identifiers consistently across Python versions."""

    try:
        tokens = tuple(tokenize.generate_tokens(io.StringIO(source).readline))
    except (IndentationError, tokenize.TokenError):
        return ()

    identifiers: list[tokenize.TokenInfo] = []
    index = 0
    while index < len(tokens):
        first = tokens[index]
        index += 1
        if first.type not in _IDENTIFIER_START_TOKEN_TYPES or not first.string.isidentifier():
            continue

        spelling = first.string
        end = first.end
        while index < len(tokens):
            following = tokens[index]
            if following.start != end or following.type not in _IDENTIFIER_PART_TOKEN_TYPES:
                break
            candidate = spelling + following.string
            if not candidate.isidentifier():
                break
            spelling = candidate
            end = following.end
            index += 1

        identifiers.append(
            tokenize.TokenInfo(tokenize.NAME, spelling, first.start, end, first.line)
        )
    return tuple(identifiers)


__all__ = ["identifier_tokens"]
