"""The safe regex subset shared by Pydantic (Rust regex), SHACL (XPath regex) and Postgres (ARE).

Model patterns are written through `safe()`, which checks the pattern and returns its canonical form.
The canonical form is what every target sees, Pydantic included, so all three read identical text.

The subset:

* literal printable ASCII characters other than metacharacters;
* the XPath single-character escapes `\\\\ \\| \\. \\? \\* \\+ \\( \\) \\{ \\} \\- \\[ \\] \\^ \\$`;
* `\\d`, canonicalized to `[0-9]` (inside a class, to `0-9`). Rust, Python and XPath give `\\d`
  Unicode meaning (Arabic-Indic digits match) and Postgres ties it to the locale, so it is never
  emitted as written;
* character classes `[...]` and `[^...]` of literals, escapes and ASCII ranges;
* capturing groups `( ... )` and alternation `|`;
* `^` at the start and `$` at the end of a top-level branch;
* quantifiers `* + ? {n} {n,} {n,m}` with bounds up to 255 (the Postgres RE_DUPMAX).

Rejected, each with a reason: `.`, other class escapes (`\\D \\w \\s \\S \\b`), `(?...)` groups and
lookaround, back references, lazy and possessive quantifiers, and non-ASCII characters.

`bounds()` also reports the shortest and longest string a pattern can match (None when unbounded),
which the SHACL generator turns into sh:maxLength.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_META = set(".^$|?*+()[]{}\\")
_ESCAPABLE = set("\\|.?*+(){}-[]^$")
_MAX_REPEAT = 255

Length = tuple[int, int | None]


class RegexSubsetError(ValueError):
    def __init__(self, pattern: str, position: int, reason: str) -> None:
        super().__init__(f"pattern {pattern!r} is outside the safe regex subset at position {position}: {reason}")
        self.pattern = pattern
        self.position = position
        self.reason = reason


def _add(a: Length, b: Length) -> Length:
    return a[0] + b[0], None if a[1] is None or b[1] is None else a[1] + b[1]


def _either(a: Length, b: Length) -> Length:
    return min(a[0], b[0]), None if a[1] is None or b[1] is None else max(a[1], b[1])


@dataclass
class _Parser:
    pattern: str
    pos: int = 0
    depth: int = 0
    out: list[str] = field(default_factory=list)

    def fail(self, reason: str) -> RegexSubsetError:
        return RegexSubsetError(self.pattern, self.pos, reason)

    def peek(self) -> str | None:
        return self.pattern[self.pos] if self.pos < len(self.pattern) else None

    def take(self) -> str:
        ch = self.pattern[self.pos]
        self.pos += 1
        return ch

    def emit(self, text: str) -> None:
        self.out.append(text)

    def regex(self) -> Length:
        length = self.branch()
        while self.peek() == "|":
            self.emit(self.take())
            length = _either(length, self.branch())
        return length

    def branch(self) -> Length:
        top = self.depth == 0
        length: Length = (0, 0)
        if self.peek() == "^":
            if not top:
                raise self.fail("^ is only allowed at the start of a top-level branch")
            self.emit(self.take())
        while (ch := self.peek()) is not None and ch not in "|)":
            if ch == "$":
                self.emit(self.take())
                if not top or self.peek() not in (None, "|"):
                    raise self.fail("$ is only allowed at the end of a top-level branch")
                return length
            start = len(self.out)
            atom = self.atom()
            length = _add(length, self.quantifier(atom, start))
        return length

    def atom(self) -> Length:
        ch = self.peek()
        assert ch is not None
        if ch == "(":
            self.emit(self.take())
            if self.peek() == "?":
                raise self.fail("(?...) groups, lookaround and inline flags are not in the subset")
            self.depth += 1
            inner = self.regex()
            self.depth -= 1
            if self.peek() != ")":
                raise self.fail("unbalanced parenthesis")
            self.emit(self.take())
            return inner
        if ch == "[":
            self.char_class()
            return (1, 1)
        if ch == "\\":
            self.emit(self.escape(in_class=False))
            return (1, 1)
        if ch == ".":
            raise self.fail("'.' matches newlines differently across dialects; use an explicit class")
        if ch == "^":
            raise self.fail("^ is only allowed at the start of a top-level branch")
        if ch in _META:
            raise self.fail(f"unexpected metacharacter {ch!r}")
        self.emit(self.literal())
        return (1, 1)

    def literal(self) -> str:
        ch = self.take()
        if not (" " <= ch <= "~"):
            self.pos -= 1
            raise self.fail(f"character {ch!r} is not printable ASCII")
        return ch

    def escape(self, in_class: bool) -> str:
        self.take()
        ch = self.peek()
        if ch is None:
            raise self.fail("dangling backslash")
        if ch == "d":
            self.take()
            return "0-9" if in_class else "[0-9]"
        if ch not in _ESCAPABLE:
            raise self.fail(f"escape \\{ch} is not an XPath single-character escape in the subset")
        return "\\" + self.take()

    def char_class(self) -> None:
        self.emit(self.take())
        if self.peek() == "^":
            self.emit(self.take())
        count = 0
        while (ch := self.peek()) != "]":
            if ch is None:
                raise self.fail("unterminated character class")
            low, low_text = self.class_char()
            self.emit(low_text)
            if self.peek() == "-":
                if low is None:
                    raise self.fail("a class escape cannot start a range")
                self.emit(self.take())
                if self.peek() == "]":
                    raise self.fail("write a literal '-' in a class as \\-")
                high, high_text = self.class_char()
                if high is None or ord(high) < ord(low):
                    raise self.fail("reversed or invalid range in character class")
                self.emit(high_text)
            count += 1
        if count == 0:
            raise self.fail("empty character class")
        self.emit(self.take())

    def class_char(self) -> tuple[str | None, str]:
        """Return (the single character, or None for a class escape such as \\d) and its canonical text."""
        ch = self.peek()
        if ch == "\\":
            text = self.escape(in_class=True)
            return (None if text == "0-9" else text[-1]), text
        if ch == "[":
            raise self.fail("'[' inside a class means subtraction in XPath; escape it")
        if ch == "-":
            raise self.fail("write a literal '-' in a class as \\-")
        lit = self.literal()
        return lit, lit

    def quantifier(self, atom: Length, start: int) -> Length:
        ch = self.peek()
        if ch == "*":
            self.emit(self.take())
            result: Length = (0, None if atom[1] != 0 else 0)
        elif ch == "+":
            self.emit(self.take())
            result = (atom[0], None if atom[1] != 0 else 0)
        elif ch == "?":
            self.emit(self.take())
            result = (0, atom[1])
        elif ch == "{":
            open_at = self.pos
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
            self.emit(self.pattern[open_at : self.pos])
            top = None if high is None or atom[1] is None else atom[1] * high
            result = (atom[0] * low, top if high is not None else (None if atom[1] != 0 else 0))
        else:
            return atom
        if self.peek() in ("?", "+"):
            raise self.fail("lazy or possessive quantifiers are not in the subset")
        if self.peek() in ("*", "+", "?", "{"):
            raise self.fail("stacked quantifiers are not in the subset")
        return result

    def number(self) -> int:
        start = self.pos
        while (ch := self.peek()) is not None and ch.isdigit():
            self.take()
        if start == self.pos:
            raise self.fail("expected a number")
        return int(self.pattern[start : self.pos])


@dataclass(frozen=True, slots=True)
class Analysis:
    canonical: str
    min_length: int
    max_length: int | None
    anchored: bool


def analyze_pattern(pattern: str) -> Analysis:
    """Check `pattern` against the subset and return its canonical form and length bounds.

    Length bounds are only meaningful for a fully anchored pattern; for an unanchored one, any string
    containing a match also matches, so the maximum is None.
    """
    parser = _Parser(pattern)
    low, high = parser.regex()
    if parser.pos != len(pattern):
        raise parser.fail("unbalanced parenthesis")
    canonical = "".join(parser.out)
    anchored = _fully_anchored(canonical)
    return Analysis(canonical, low if anchored else 0, high if anchored else None, anchored)


def _fully_anchored(canonical: str) -> bool:
    branches, depth, current = [], 0, []
    escaped = in_class = False
    for ch in canonical:
        if escaped:
            current.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
        elif ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif not in_class and ch == "(":
            depth += 1
        elif not in_class and ch == ")":
            depth -= 1
        elif not in_class and ch == "|" and depth == 0:
            branches.append("".join(current))
            current = []
            continue
        current.append(ch)
    branches.append("".join(current))
    return all(b.startswith("^") and b.endswith("$") and not b.endswith("\\$") for b in branches)


def check_pattern(pattern: str) -> None:
    """Raise RegexSubsetError unless `pattern` is in the subset and already in canonical form."""
    analysis = analyze_pattern(pattern)
    if analysis.canonical != pattern:
        raise RegexSubsetError(
            pattern,
            0,
            f"not in canonical form; write safe({pattern!r}), which every target reads as {analysis.canonical!r}",
        )


def canonicalize(pattern: str) -> str:
    return analyze_pattern(pattern).canonical


def safe(pattern: str) -> str:
    """Use in models: `Field(pattern=safe(r"^\\d{10}$"))`. Fails at import time if outside the subset."""
    return canonicalize(pattern)
