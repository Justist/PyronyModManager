"""Lossless-enough Clausewitz parser used by Pyrony's semantic layer.

The AST preserves scalar kinds, quoting, comments, duplicate statements and
source positions.  Whitespace is canonicalised when serialising; comments and
statement order are retained.  Invalid input raises :class:`CWParseError`
instead of producing a partial tree.
"""

import codecs
import re
from collections import defaultdict
from dataclasses import dataclass, field
from enum import auto, Enum
from pathlib import Path
from typing import Iterable, TypeAlias


class CWParseError(ValueError):
    """A syntax error with source location information."""
    
    def __init__(self, message: str, line: int, column: int,
                 path: Path | None = None) -> None:
        location = f"{path or '<text>'}:{line}:{column}"
        super().__init__(f"{location}: {message}")
        self.message = message
        self.line = line
        self.column = column
        self.path = path


class ScalarKind(Enum):
    IDENTIFIER = auto()
    STRING = auto()
    INTEGER = auto()
    DECIMAL = auto()
    BOOLEAN = auto()
    DATE = auto()
    METAPROGRAM = auto()


@dataclass(frozen=True, slots=True)
class CWScalar:
    text: str
    kind: ScalarKind = ScalarKind.IDENTIFIER
    quoted: bool = False
    line: int = 0
    column: int = 0
    
    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True, slots=True)
class CWComment:
    text: str
    line: int = 0
    column: int = 0


@dataclass(slots=True)
class CWRaw:
    value: CWScalar
    
    @property
    def text(self) -> str:
        return self.value.text
    
    def __str__(self) -> str:
        return self.value.text


@dataclass(slots=True)
class CWBlock:
    items: list["CWItem"] = field(default_factory=list)


@dataclass(slots=True)
class CWPair:
    key: CWScalar | str
    op: str
    value: "CWValue"
    line: int = 0
    column: int = 0
    
    def __post_init__(self) -> None:
        if isinstance(self.key, str):
            self.key = CWScalar(self.key, ScalarKind.IDENTIFIER,
                                line=self.line, column=self.column)
    
    @property
    def key_text(self) -> str:
        assert isinstance(self.key, CWScalar)
        return self.key.text


CWValue: TypeAlias = CWBlock | CWScalar | str
CWItem: TypeAlias = CWPair | CWRaw | CWBlock | CWComment

_ID_KEYS = frozenset({
    "id", "name", "token", "tag", "key", "type", "title", "event_id",
    "class", "category",
})


@dataclass(slots=True)
class CWFile:
    path: Path
    root: CWBlock
    had_bom: bool = False
    
    def top_pairs(self) -> list[CWPair]:
        return [item for item in self.root.items if isinstance(item, CWPair)]
    
    def definitions(self) -> dict[str, CWPair]:
        """Return compatibility definition keys while preserving ID case."""
        result: dict[str, CWPair] = {}
        unnamed: dict[str, int] = defaultdict(int)
        for pair in self.top_pairs():
            key = pair.key_text
            if isinstance(pair.value, CWBlock):
                identity = inner_id(pair.value)
                if identity is not None:
                    definition_key = f"{key}.{identity}"
                else:
                    definition_key = f"{key}@{unnamed[key]}"
                    unnamed[key] += 1
            else:
                definition_key = key
            result[definition_key] = pair
        return result
    
    def definition_names(self) -> set[str]:
        return set(self.definitions())


def scalar_text(value: CWValue) -> str | None:
    if isinstance(value, CWScalar):
        return value.text
    return value if isinstance(value, str) else None


def inner_id(block: CWBlock, names: Iterable[str] = _ID_KEYS) -> str | None:
    allowed = {name.casefold() for name in names}
    for item in block.items:
        if (isinstance(item, CWPair)
                and item.key_text.casefold() in allowed):
            value = scalar_text(item.value)
            if value is not None:
                return value
    return None


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    text: str
    line: int
    column: int


_NUMBER = re.compile(r"-?(?:\d+\.\d+|\d+)")
_DATE = re.compile(r"\d{1,4}\.\d{1,2}\.\d{1,2}(?![.\w])")
_IDENT_END = frozenset(" \t\r\n{}=<>!?#\"")
_OPERATORS = (">=", "<=", "!=", "==", "?=", "=", ">", "<")


def _advance(text: str, line: int, column: int) -> tuple[int, int]:
    if "\n" not in text:
        return line, column + len(text)
    return line + text.count("\n"), len(text.rsplit("\n", 1)[-1]) + 1


def _tokenize(source: str, path: Path | None) -> list[_Token]:
    tokens: list[_Token] = []
    index = 0
    line = column = 1
    length = len(source)
    while index < length:
        start_line, start_column = line, column
        char = source[index]
        if char.isspace():
            end = index + 1
            while end < length and source[end].isspace():
                end += 1
            text = source[index:end]
            line, column = _advance(text, line, column)
            index = end
            continue
        if char == "#":
            end = source.find("\n", index)
            if end < 0:
                end = length
            text = source[index:end]
            tokens.append(_Token("COMMENT", text, line, column))
            line, column = _advance(text, line, column)
            index = end
            continue
        if char == '"':
            end = index + 1
            escaped = False
            while end < length:
                current = source[end]
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    end += 1
                    break
                elif current == "\n":
                    raise CWParseError("newline in quoted string", line, column, path)
                end += 1
            else:
                raise CWParseError("unterminated quoted string", line, column, path)
            text = source[index:end]
            tokens.append(_Token("STRING", text, start_line, start_column))
            line, column = _advance(text, line, column)
            index = end
            continue
        if source.startswith("@[", index):
            end = index + 2
            depth = 1
            quoted = False
            escaped = False
            while end < length and depth:
                current = source[end]
                if quoted:
                    if escaped:
                        escaped = False
                    elif current == "\\":
                        escaped = True
                    elif current == '"':
                        quoted = False
                elif current == '"':
                    quoted = True
                elif current == "[":
                    depth += 1
                elif current == "]":
                    depth -= 1
                end += 1
            if depth:
                raise CWParseError("unterminated metaprogram expression",
                                   line, column, path)
            text = source[index:end]
            tokens.append(_Token("METAPROGRAM", text, start_line, start_column))
            line, column = _advance(text, line, column)
            index = end
            continue
        if operator := next(
            (op for op in _OPERATORS if source.startswith(op, index)), None
        ):
            tokens.append(_Token("OP", operator, line, column))
            index += len(operator)
            column += len(operator)
            continue
        if char in "{}":
            tokens.append(_Token("LBRACE" if char == "{" else "RBRACE",
                                 char, line, column))
            index += 1
            column += 1
            continue
        if date_match := _DATE.match(source, index):
            text = date_match.group()
            tokens.append(_Token("DATE", text, line, column))
            index += len(text)
            column += len(text)
            continue
        if number_match := _NUMBER.match(source, index):
            text = number_match.group()
            tokens.append(_Token("DECIMAL" if "." in text else "INTEGER",
                                 text, line, column))
            index += len(text)
            column += len(text)
            continue
        end = index
        while (end < length and source[end] not in _IDENT_END
               and not any(source.startswith(op, end) for op in _OPERATORS)):
            end += 1
        if end == index:
            raise CWParseError(f"unexpected character {char!r}", line, column, path)
        text = source[index:end]
        tokens.append(_Token("IDENT", text, line, column))
        index = end
        column += len(text)
    return tokens

def _decode_string(token: _Token, path: Path | None) -> str:
    try:
        return codecs.decode(token.text[1:-1], "unicode_escape")
    except UnicodeDecodeError as error:
        raise CWParseError("invalid string escape", token.line, token.column,
                           path) from error


def _scalar(token: _Token, path: Path | None = None) -> CWScalar:
    if token.kind == "STRING":
        return CWScalar(_decode_string(token, path), ScalarKind.STRING, True,
                        token.line, token.column)
    kinds = {
        "INTEGER": ScalarKind.INTEGER,
        "DECIMAL": ScalarKind.DECIMAL,
        "DATE": ScalarKind.DATE,
        "METAPROGRAM": ScalarKind.METAPROGRAM,
    }
    kind = kinds.get(token.kind, ScalarKind.IDENTIFIER)
    if token.kind == "IDENT" and token.text.casefold() in {"yes", "no"}:
        kind = ScalarKind.BOOLEAN
    return CWScalar(token.text, kind, False, token.line, token.column)


class _Parser:
    def __init__(self, tokens: list[_Token], path: Path | None) -> None:
        self.tokens = tokens
        self.path = path
        self.index = 0
    
    def peek(self, offset: int = 0) -> _Token | None:
        index = self.index + offset
        return self.tokens[index] if index < len(self.tokens) else None
    
    def take(self) -> _Token:
        token = self.peek()
        if token is None:
            raise CWParseError("unexpected end of input", 1, 1, self.path)
        self.index += 1
        return token
    
    def parse(self) -> CWBlock:
        root = self.parse_block(root=True)
        if self.peek() is not None:
            token = self.peek()
            assert token is not None
            raise CWParseError("unexpected trailing input", token.line,
                               token.column, self.path)
        return root
    
    def parse_block(self, root: bool = False,
                    opener: _Token | None = None) -> CWBlock:
        items: list[CWItem] = []
        while (token := self.peek()) is not None:
            if token.kind == "RBRACE":
                if root:
                    raise CWParseError("unmatched closing brace", token.line,
                                       token.column, self.path)
                self.take()
                return CWBlock(items)
            if token.kind == "COMMENT":
                self.take()
                items.append(CWComment(token.text, token.line, token.column))
                continue
            if token.kind == "LBRACE":
                self.take()
                items.append(self.parse_block(opener=token))
                continue
            if token.kind in {"IDENT", "STRING", "INTEGER", "DECIMAL",
                              "DATE", "METAPROGRAM"}:
                if self.peek(1) is not None and self.peek(1).kind == "OP":
                    key_token = self.take()
                    operator = self.take()
                    value = self.parse_value(operator)
                    items.append(CWPair(_scalar(key_token, self.path), operator.text,
                                        value, key_token.line, key_token.column))
                else:
                    items.append(CWRaw(_scalar(self.take(), self.path)))
                continue
            raise CWParseError(f"unexpected token {token.text!r}", token.line,
                               token.column, self.path)
        if not root:
            line = opener.line if opener else 1
            column = opener.column if opener else 1
            raise CWParseError("unclosed block", line, column, self.path)
        return CWBlock(items)
    
    def parse_value(self, operator: _Token) -> CWValue:
        while self.peek() is not None and self.peek().kind == "COMMENT":
            self.take()
        token = self.peek()
        if token is None:
            raise CWParseError(f"missing value after {operator.text!r}",
                               operator.line, operator.column, self.path)
        if token.kind == "LBRACE":
            self.take()
            return self.parse_block(opener=token)
        if token.kind in {"IDENT", "STRING", "INTEGER", "DECIMAL", "DATE",
                          "METAPROGRAM"}:
            return _scalar(self.take(), self.path)
        raise CWParseError("expected scalar or block", token.line, token.column,
                           self.path)


def _escape_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def unparse_scalar(value: CWScalar | str) -> str:
    if isinstance(value, str):
        value = CWScalar(value)
    if value.quoted or value.kind is ScalarKind.STRING:
        return f'"{_escape_string(value.text)}"'
    return value.text


def unparse(node: CWValue | CWRaw | CWComment, depth: int = 0) -> str:
    if isinstance(node, (CWScalar, str)):
        return unparse_scalar(node)
    if isinstance(node, CWRaw):
        return unparse_scalar(node.value)
    if isinstance(node, CWComment):
        return node.text
    if not isinstance(node, CWBlock):
        raise TypeError(type(node))
    pad = "\t" * depth
    lines = ["{"]
    for item in node.items:
        if isinstance(item, CWPair):
            lines.append(f"{pad}\t{unparse_pair(item, depth + 1)}")
        elif isinstance(item, CWBlock):
            nested = unparse(item, depth + 1)
            lines.extend(f"{pad}\t{line}" for line in nested.splitlines())
        else:
            lines.append(f"{pad}\t{unparse(item, depth + 1)}")
    lines.append(f"{pad}}}")
    return "\n".join(lines)


def unparse_pair(pair: CWPair, depth: int = 0) -> str:
    return (f"{unparse_scalar(pair.key)} {pair.op} "
            f"{unparse(pair.value, depth)}")


def parse_text(text: str, path: Path | None = None) -> CWFile:
    had_bom = text.startswith("\ufeff")
    if had_bom:
        text = text[1:]
    return CWFile(path or Path(""), _Parser(_tokenize(text, path), path).parse(),
                  had_bom)


def parse_file(path: Path) -> CWFile:
    return parse_text(path.read_text(encoding="utf-8-sig"), path)
