"""
pmm_clausewitz
==============
Tokeniser, parser, AST, serialiser, and definition-extractor for
the Clausewitz scripting language used by all modern Paradox games.

Clausewitz syntax in brief
--------------------------
  key = value                     # scalar assignment
  key = { … }                     # block assignment
  key >= value                    # comparative assignment (triggers/conditions)
  { value value … }               # bare list (inside a block)
  # comment to end of line

Public API
----------
  parse_file(path)  -> CWFile
  parse_text(text, path?) -> CWFile

  CWFile.top_pairs()   -> list[CWPair]
  CWFile.definitions() -> dict[str, CWPair]
      Stable key format:
        named block   →  "key.inner_id"   e.g. "country_event.100"
        unnamed block →  "key@N"          N = per-key positional index
        scalar pair   →  "key"

  unparse(node, depth?) -> str
  unparse_pair(pair)    -> str
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Union


# ── AST nodes ─────────────────────────────────────────────────────────────────

class CWRaw(str):
   """Bare value inside a block (NUMBER/DATE/IDENT etc. not attached to a key)."""
   pass


Value = Union["CWBlock", str]


@dataclass
class CWBlock:
   """A { … } block; items are CWPair nodes or bare values."""
   items: List[Union["CWPair", CWRaw, "CWBlock"]] = field(default_factory=list)


@dataclass
class CWPair:
   """key OP value  (OP is usually '=', but may be >, <, >=, <=, !=)."""
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
      Map each top-level definition to a stable string key.

      Key generation rules (in priority order):
        1. Named block  — block contains an inner 'id', 'name', 'token',
                          'tag', 'key', 'type', or 'title' key
                          → "outer_key.inner_value"
                          e.g.  country_event = { id = 100 }  →  "country_event.100"

        2. Unnamed block — block has no identity key
                          → "outer_key@N"  where N is a per-outer-key counter
                          Positional index (not line number) so definitions
                          stay comparable across files that differ only in
                          whitespace / comments.

        3. Scalar pair  → "outer_key"

      Last definition with the same key wins within a single file,
      mirroring Clausewitz load order semantics.
      """
      result: Dict[str, CWPair] = {}
      unnamed_counts: Dict[str, int] = defaultdict(int)
      for pair in self.top_pairs():
         if isinstance(pair.value, CWBlock):
            if inner := _inner_id(pair.value):
               def_key = f"{pair.key}.{inner}"
            else:
               n = unnamed_counts[pair.key]
               unnamed_counts[pair.key] += 1
               def_key = f"{pair.key}@{n}"
         else:
            def_key = pair.key
         result[def_key] = pair  # last one wins
      return result

   def definition_names(self) -> set[str]:
      """Return just the set of definition keys (cheaper than full definitions())."""
      return set(self.definitions().keys())


# ── identity-key detection ────────────────────────────────────────────────────

# Keys inside a block that unambiguously identify the definition.
# Ordered so the most common ones are checked first.
_ID_KEYS = frozenset({
   "id",  # events, provinces, …
   "name",  # buildings, technologies, …
   "token",  # decisions (HOI4, CK3)
   "tag",  # country tags (EU4, HOI4)
   "key",  # modifiers, localisation
   "type",  # component_templates (Stellaris)
   "title",  # laws (Vic3)
})


def _inner_id(block: CWBlock) -> str | None:
   """Return the string value of the first identity key inside a block."""
   for item in block.items:
      if isinstance(item, CWPair) and item.key in _ID_KEYS:
         if isinstance(item.value, str):
            return item.value
   return None


# ── Tokeniser ─────────────────────────────────────────────────────────────────

_PAT = re.compile(
   r'(?P<STRING>"[^"]*")'  # "quoted string"
   r'|(?P<DATE>\b\d{1,4}\.\d{1,2}\.\d{1,2}\b)'  # date  (before NUMBER)
   r'|(?P<NUMBER>-?\d+\.?\d*)'  # integer / float
   r'|(?P<OP>>=|<=|!=|[=<>])'  # operator
   r'|(?P<LBRACE>{)'
   r'|(?P<RBRACE>})'
   r'|(?P<IDENT>[@\w][.\w:-]*)'  # identifiers, @vars
   r'|(?P<SKIP>#[^\n]*|[ \t\r\n]+|.)',  # comments, whitespace, junk
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

   def _eat(self) -> _Tok:
      tok = self._peek()
      if tok is None:
         # Parser logic should prevent this; this is a sanity guard.
         raise RuntimeError("Attempted to consume token past end of stream")
      self._i += 1
      return tok

   def parse_block(self, *, root: bool = False) -> CWBlock:
      block = CWBlock()
      while True:
         tok = self._peek()
         if tok is None:
            break
         if tok.kind == "RBRACE":
            self._eat()
            if not root:
               break
         elif tok.kind in ("IDENT", "STRING"):
            nxt = self._t[self._i + 1] if self._i + 1 < len(self._t) else None
            if nxt is not None and nxt.kind == "OP":
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
               block.items.append(CWRaw(tok.val.strip('"')))
               self._eat()
         elif tok.kind in ("NUMBER", "DATE"):
            block.items.append(CWRaw(tok.val))
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


# ── Serialiser  (AST → canonical Clausewitz text) ────────────────────────────

def unparse(node: Value, depth: int = 0) -> str:
   """Convert an AST node back to canonical Clausewitz text."""
   if isinstance(node, str):
      # Covers both plain str and CWRaw
      return f'"{node}"' if (" " in node or not node) else node

   # At this point node must be a CWBlock
   pad = "\t" * depth
   lines: List[str] = ["{"]
   for item in node.items:
      if isinstance(item, CWPair):
         lines.append(
            f"{pad}\t{item.key} {item.op} "
            f"{unparse(item.value, depth + 1)}"
         )
      elif isinstance(item, CWBlock):
         nested = unparse(item, depth)
         lines.extend(f"{pad}\t{line}" for line in nested.splitlines())
      else:
         # item is CWRaw (a str subclass)
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
   return CWFile(path=path or Path(""), root=root)


def parse_file(path: Path) -> CWFile:
   """Read and parse a Clausewitz script file (handles UTF-8 BOM)."""
   text = path.read_text(encoding="utf-8-sig", errors="replace")
   return parse_text(text, path)
