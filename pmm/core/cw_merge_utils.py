from typing import List, Set, Tuple

from pmm.core.clausewitz import CWBlock, CWPair, unparse, unparse_pair


def merge_block_items_union(blocks: List[CWBlock]) -> Tuple[CWBlock, int]:
   """
   Union all inner items from the given CWBlock values.

   Duplicate items (same text) are deduplicated.
   Items are emitted in the order they first appear across blocks.

   Returns (merged_block, unique_count).
   """
   seen: Set[str] = set()
   merged_items: list = []

   for b in blocks:
      for item in b.items:
         # Normalize to Value (CWBlock | str) for unparse:
         if isinstance(item, CWBlock):
            value_for_unparse = item
         elif isinstance(item, CWPair):
            text = unparse_pair(item)
            if text in seen:
               continue
            seen.add(text)
            merged_items.append(item)
            continue
         else:  # CWRaw or plain str
            value_for_unparse = str(item)

         text = unparse(value_for_unparse)
         if text not in seen:
            seen.add(text)
            merged_items.append(item)

   return CWBlock(items=merged_items), len(seen)
