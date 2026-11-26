"""The safe regex subset shared by Pydantic (Rust regex), SHACL (XPath regex) and Postgres (ARE).

A pattern outside the subset is rejected with a positioned error. The subset is deliberately small:

* literal printable ASCII characters other than metacharacters;
* the XPath single-character escapes `\\ \\| \\. \\? \\* \\+ \\( \\) \\{ \\} \\- \\[ \\] \\^ \\$`;
* character classes `[...]` and `[^...]` of literals, escapes and ASCII ranges;
* capturing groups `( ... )` and alternation `|`;
* `^` at the start and `$` at the end of a top-level branch;
* quantifiers `* + ? {n} {n,} {n,m}` with bounds up to 255 (the Postgres RE_DUPMAX).

Rejected, each with a reason: `.` (Postgres lets dot match a newline, XPath excludes CR as well),
class escapes such as `\\d \\w \\s` (Unicode-aware in Rust, Python and XPath, locale-dependent in
Postgres), `(?...)` groups and lookaround, back references, lazy and possessive quantifiers, and
non-ASCII characters.
"""

from __future__ import annotations

from dataclasses import dataclass

_META = set(".^$|?*+()[]{}\\")
_ESCAPABLE = set("\\|.?*+(){}-[]^$")
_MAX_REPEAT = 255


class RegexSubsetError(ValueError):
    def __init__(self, pattern: str, position: int, reason: str) -> None:
        super().__init__(f"pattern {pattern!r} is outside the safe regex subset at position {position}: {reason}")
        self.pattern = pattern
        self.position = position
        self.reason = reason


@dataclass
class _Parser:
    pattern: str
    pos: int = 0
    depth: int = 0

    def fail(self, reason: str) -> RegexSubsetError:
        return RegexSubsetError(self.pattern, self.pos, reason)

    def peek(self) -> str | None:
        return self.pattern[self.pos] if self.pos < len(self.pattern) else None

    def take(self) -> str:
        ch = self.pattern[self.pos]
        self.pos += 1
        return ch

    def regex(self) -> None:
        self.branch()
        while self.peek() == "|":
            self.take()
            self.branch()

    def branch(self) -> None:
        top = self.depth == 0
        if self.peek() == "^":
            if not top:
                raise self.fail("^ is only allowed at the start of a top-level branch")
            self.take()
        while (ch := self.peek()) is not None and ch not in "|)":
            if ch == "$":
                self.take()
                if not top or self.peek() not in (None, "|"):
                    raise self.fail("$ is only allowed at the end of a top-level branch")
                return
            self.atom()
            self.quantifier()

    def atom(self) -> None:
        ch = self.peek()
        assert ch is not None
        if ch == "(":
            self.take()
            if self.peek() == "?":
                raise self.fail("(?...) groups, lookaround and inline flags are not in the subset")
            self.depth += 1
            self.regex()
            self.depth -= 1
            if self.peek() != ")":
                raise self.fail("unbalanced parenthesis")
            self.take()
        elif ch == "[":
            self.char_class()
        elif ch == "\\":
            self.escape()
        elif ch == ".":
            raise self.fail("'.' matches newlines differently across dialects; use an explicit class")
        elif ch == "^":
            raise self.fail("^ is only allowed at the start of a top-level branch")
        elif ch in _META:
            raise self.fail(f"unexpected metacharacter {ch!r}")
        else:
            self.literal()

    def literal(self) -> str:
        ch = self.take()
        if not (" " <= ch <= "~"):
            self.pos -= 1
            raise self.fail(f"character {ch!r} is not printable ASCII")
        return ch

    def escape(self) -> str:
        self.take()
        ch = self.peek()
        if ch is None:
            raise self.fail("dangling backslash")
        if ch not in _ESCAPABLE:
            raise self.fail(f"escape \\{ch} is not an XPath single-character escape in the subset")
        return self.take()

    def char_class(self) -> None:
        self.take()
        if self.peek() == "^":
            self.take()
        count = 0
        while (ch := self.peek()) != "]":
            if ch is None:
                raise self.fail("unterminated character class")
            low = self.class_char()
            if self.peek() == "-":
                self.take()
                if self.peek() == "]":
                    raise self.fail("write a literal '-' in a class as \\-")
                high = self.class_char()
                if ord(high) < ord(low):
                    raise self.fail("reversed range in character class")
            count += 1
        if count == 0:
            raise self.fail("empty character class")
        self.take()

    def class_char(self) -> str:
        ch = self.peek()
        if ch == "\\":
            return self.escape()
        if ch == "[":
            raise self.fail("'[' inside a class means subtraction in XPath; escape it")
        if ch == "-":
            raise self.fail("write a literal '-' in a class as \\-")
        return self.literal()

    def quantifier(self) -> None:
        ch = self.peek()
        if ch in ("*", "+", "?"):
            self.take()
        elif ch == "{":
            self.take()
            low = self.number()
            high: int | None = low
            if self.peek() == ",":
                self.take()
                high = self.number() if self.peek() != "}" else None
            if self.peek() != "}":
                raise self.fail("malformed {n,m} quantifier")
            self.take()
            if max(low, high or 0) > _MAX_REPEAT:
                raise self.fail(f"repeat bounds above {_MAX_REPEAT} are rejected by Postgres")
            if high is not None and high < low:
                raise self.fail("quantifier upper bound below lower bound")
        else:
            return
        if self.peek() in ("?", "+"):
            raise self.fail("lazy or possessive quantifiers are not in the subset")
        if self.peek() in ("*", "+", "?", "{"):
            raise self.fail("stacked quantifiers are not in the subset")

    def number(self) -> int:
        start = self.pos
        while (ch := self.peek()) is not None and ch.isdigit():
            self.take()
        if start == self.pos:
            raise self.fail("expected a number")
        return int(self.pattern[start : self.pos])


def check_pattern(pattern: str) -> None:
    """Raise RegexSubsetError if `pattern` is outside the safe subset."""
    parser = _Parser(pattern)
    parser.regex()
    if parser.pos != len(pattern):
        raise parser.fail("unbalanced parenthesis")
