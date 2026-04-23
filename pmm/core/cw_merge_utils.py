# pmm/core/cw_merge_utils.py
from typing import List, Set, Tuple, Dict

from pmm.core.clausewitz import CWBlock, CWPair, CWRaw, unparse, unparse_pair


def merge_block_items_union(blocks: List[CWBlock]) -> Tuple[CWBlock, int]:
   """
   Union all inner items from the given CWBlock values.

   Duplicate items (same text) are deduplicated.
   Items are emitted in the order they first appear across blocks.

   Returns (merged_block, unique_count).

   Use this for bare-list blocks (e.g. on_actions event ID lists).
   For CWPair-keyed blocks (e.g. modifier blocks), use merge_block_pairs_by_key.
   """
   seen: Set[str] = set()
   merged_items: list = []

   for b in blocks:
      for item in b.items:
         if isinstance(item, CWBlock):
            # Nested anonymous block — deduplicate by serialised text.
            text = unparse(item)
            if text in seen:
               continue
            seen.add(text)
            merged_items.append(item)
         elif isinstance(item, CWPair):
            text = unparse_pair(item)
            if text in seen:
               continue
            seen.add(text)
            merged_items.append(item)
         else:
            # CWRaw bare value
            text = str(item)
            if text in seen:
               continue
            seen.add(text)
            merged_items.append(item)

   merged = CWBlock(items=merged_items)
   return merged, len(merged_items)


def merge_block_pairs_by_key(blocks: List[CWBlock]) -> Tuple[CWBlock, int]:
   """
   Merge CWPair-keyed blocks from multiple mods using key-union / last-wins.

   Algorithm:
     1. Collect ALL keys that appear in ANY block, preserving first-seen order.
     2. For each key, the value from the LAST block that contains it wins
        (load-order semantics: last mod has highest priority).
     3. Non-CWPair items (bare CWRaw values, anonymous CWBlocks) are
        appended after all keyed pairs, deduplicated by text.

   This is correct for modifier/attribute blocks where mod B may only
   override a subset of the keys that mod A defines — keys not present
   in mod B are taken from mod A rather than being dropped.

   Returns (merged_block, pair_count).
   """
   # Ordered dict: key → latest CWPair seen (last block wins per key)
   keyed: Dict[str, CWPair] = {}
   key_order: List[str] = []  # preserves first-seen insertion order

   bare_seen: Set[str] = set()
   bare_items: list = []  # CWRaw / anonymous CWBlock items

   for b in blocks:
      for item in b.items:
         if isinstance(item, CWPair):
            if item.key not in keyed:
               key_order.append(item.key)
            keyed[item.key] = item  # last block's version wins
         else:
            text = unparse(item) if isinstance(item, CWBlock) else str(item)
            if text not in bare_seen:
               bare_seen.add(text)
               bare_items.append(item)

   merged_items: list = [keyed[k] for k in key_order] + bare_items
   return CWBlock(items=merged_items), len(keyed)