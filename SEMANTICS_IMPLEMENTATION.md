# Stellaris semantic interpretation

This source package separates Clausewitz syntax, game-aware context, Boolean
equivalence, definition indexing, and merge policy. The runtime is pure Python;
the profile format is designed for later deterministic generation from CWTools
and `cwtools-stellaris-config`.

## Removed files

- `pmm/core/cw_merge_utils.py`: context-free union and deduplication are unsafe
  for repeated effects and ordered entries. All callers were migrated.
- `pmm/core/semantic_worker.py`: `patch_worker.py` is the active UI worker. The
  duplicate worker imported private implementation details and had no callers.

## Retained files

- `pmm/core/clausewitz.py`: now the typed syntax layer.
- `pmm/core/semantic.py`: preserves the UI-facing `build_patch_mod`,
  `build_merged_mod`, `PatchResult`, and `MergeNote` API.
- `pmm/core/patch_solver.py`: remains the manual conflict solver, but its unsafe
  context-free `MERGE_ALL` path is removed.

## Profile limitation

`pmm/data/semantics/stellaris/profile.json` is a bootstrap profile, not a full
copy of the community CWT rules. Its provenance field deliberately says
`bootstrap-profile-replace-with-cwtools-export`.

## Updated implementation order

1. Run the supplied tests and existing UI smoke tests.
2. Add fixtures from real vanilla and workshop Stellaris files.
3. Build a deterministic .NET profile compiler pinned to CWTools and
   `cwtools-stellaris-config` revisions.
4. Replace bootstrap JSON with generated paths, aliases, `nameField`,
   cardinality, scopes, and context roles.
5. Add profile provenance/regeneration checks in CI.
6. Resolve nested CWT contexts for triggers, effects, modifiers, ordered
   sequences, and genuine sets.
7. Feed playset-wide definition collisions into the normal conflict UI.
8. Display structured semantic decisions and their sources.
9. Add explicit user choices for Boolean composition and effect ordering.
10. Add versioned, user-approved field policies for arithmetic/set operations.
11. Validate output through a persistent CWTools sidecar.
12. Add other games only through independent generated profiles and policies.

## Safety invariants

- Only known keywords compare case-insensitively; identifiers retain case.
- Repeated effects and ordered entries are never automatically deduplicated.
- Numeric types do not imply ADD, MIN, or MAX.
- Malformed input raises an error instead of producing a partial patch.
- Equivalence can suppress a false conflict but does not rewrite source.
- Unknown content follows normal load order rather than unsafe synthesis.
