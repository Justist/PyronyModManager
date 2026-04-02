import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Union

# ── AST nodes ─────────────────────────────────────────────────────────────────

Value = Union["CWBlock", str]


@dataclass
class CWBlock:
   """A { … } block; items are CWPair nodes or bare string values (list entries)."""
   items: List[Union[CWPair, str]] = field(default_factory=list)


@dataclass
class CWPair:
   """key OP value   (OP is usually '=', but can be >, <, >=, <=, !=)."""
   key: str
   op: str
   value: Value
   line: int = 0


@dataclass
class CWFile:
   path: Path
   root: CWBlock

   def top_pairs(self) -> List[CWPair]:
      """All top-level CWPair nodes (ignores bare list values)."""
      return [x for x in self.root.items if isinstance(x, CWPair)]

   def definitions(self) -> Dict[str, CWPair]:
      """
      Map each top-level named definition to a stable string key.

      Blocks that contain an inner 'id' or 'name' key use that value as
      their definition key (e.g. country_event.events.1).
      Plain key=value pairs at the top level use their own key.
      Last definition wins within a single file, mirroring Clausewitz.
      """
      result: Dict[str, CWPair] = {}
      for pair in self.top_pairs():
         if isinstance(pair.value, CWBlock):
            inner_id = _inner_id(pair.value)
            def_key = f"{pair.key}.{inner_id}" if inner_id else f"{pair.key}@{pair.line}"
         else:
            def_key = pair.key
         result[def_key] = pair  # last one wins
      return result


def _inner_id(block: CWBlock) -> str | CWBlock | None:
   """Return the value of the first 'id' or 'name' key inside a block."""
   return next(
       (item.value
        for item in block.items if isinstance(item, CWPair) and item.key in (
            "id", "name") and isinstance(item.value, str)),
       None,
   )


# ── Tokeniser ─────────────────────────────────────────────────────────────────

_PAT = re.compile(
   r'(?P<STRING>"[^"]*")'  # "quoted string"
   r'|(?P<DATE>\b\d{1,4}\.\d{1,2}\.\d{1,2}\b)'  # date  (before NUMBER to avoid partial match)
   r'|(?P<NUMBER>-?\d+\.?\d*)'  # integer or float
   r'|(?P<OP>>=|<=|!=|[=<>])'  # operator
   r'|(?P<LBRACE>{)'
   r'|(?P<RBRACE>})'
   r'|(?P<IDENT>[@\w][.\w:-]*)'  # identifiers, @variables, dotted names
   r'|(?P<SKIP>#[^\n]*|[ \t\r\n]+|.)',  # comments, whitespace, junk — all skipped
)


@dataclass(slots=True)
class _Tok:
   kind: str
   val: str
   line: int


def _tokenize(src: str) -> List[_Tok]:
   toks: List[_Tok] = []
   line = 1
   for m in _PAT.finditer(src):
      kind = m.lastgroup
      val = m.group()
      if kind and kind != "SKIP":
         toks.append(_Tok(kind, val, line))
      line += val.count("\n")
   return toks


# ── Parser ────────────────────────────────────────────────────────────────────

class _Parser:
   __slots__ = ("_t", "_i")

   def __init__(self, tokens: List[_Tok]) -> None:
      self._t = tokens
      self._i = 0

   def _peek(self) -> _Tok | None:
      return self._t[self._i] if self._i < len(self._t) else None

   def _eat(self) -> _Tok | None:
      tok = self._peek()
      self._i += 1
      return tok

   def parse_block(self, *, root: bool = False) -> CWBlock:
      block = CWBlock()
      while True:
         tok = self._peek()
         if tok is None:
            break
         if tok.kind == "RBRACE":
            self._eat()  # consume '}' (for root blocks this is a stray brace)
            if not root:
               break
         elif tok.kind in ("IDENT", "STRING"):
            # key = value   OR   bare list entry
            nxt = self._t[self._i + 1] if self._i + 1 < len(self._t) else None
            if nxt and nxt.kind == "OP":
               key_tok = self._eat()
               op_tok = self._eat()
               val = self._parse_value()
               block.items.append(
                  CWPair(
                     key=key_tok.val.strip('"'),
                     op=op_tok.val,
                     value=val,
                     line=key_tok.line,
                  )
               )
            else:
               block.items.append(tok.val.strip('"'))
               self._eat()
         elif tok.kind in ("NUMBER", "DATE"):
            block.items.append(tok.val)
            self._eat()
         elif tok.kind == "LBRACE":
            self._eat()
            block.items.append(self.parse_block())
         else:
            self._eat()
      return block

   def _parse_value(self) -> Value:
      tok = self._peek()
      if tok is None:
         return ""
      if tok.kind == "LBRACE":
         self._eat()
         return self.parse_block()
      self._eat()
      return tok.val.strip('"')


# ── Serialiser (AST → text) ───────────────────────────────────────────────────

def unparse(node: Value, depth: int = 0) -> str:
   """Convert an AST node back to canonical Clausewitz text."""
   if isinstance(node, str):
      return f'"{node}"' if " " in node or not node else node
   pad = "\t" * depth
   lines: list[str] = ["{"]
   for item in node.items:
      if isinstance(item, CWPair):
         lines.append(f"{pad}\t{item.key} {item.op} {unparse(item.value, depth + 1)}")
      else:
         lines.append(f"{pad}\t{item}")
   lines.append(f"{pad}}}")
   return "\n".join(lines)


def unparse_pair(pair: CWPair) -> str:
   """Return canonical text for a single CWPair (used in diffs)."""
   return f"{pair.key} {pair.op} {unparse(pair.value)}"


# ── Public API ────────────────────────────────────────────────────────────────

def parse_text(text: str, path: Path | None = None) -> CWFile:
   """Parse Clausewitz script from a string."""
   tokens = _tokenize(text)
   root = _Parser(tokens).parse_block(root=True)
   return CWFile(path=path or Path("<string>"), root=root)


def parse_file(path: Path) -> CWFile:
   """Read and parse a Clausewitz script file (handles UTF-8 BOM)."""
   text = path.read_text(encoding="utf-8-sig", errors="replace")
   return parse_text(text, path)
