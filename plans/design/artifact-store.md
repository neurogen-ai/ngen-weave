# Artifact store

Decisions and reasons for the v0.1 content-addressed artifact slice: blob
addressing, sidecar metadata, and what the engine persists on each
activation. The how-it-works detail lives in `/docs/engine/execution.md`;
this doc records why it is that way.

## Content addressing

A declared field's value serializes with `json.dumps(value,
sort_keys=True, ensure_ascii=False)` and the sha256 of those bytes is the
blob's address under `.ngen-weave/projects/<project>/<sha256>`. Writes are
idempotent: identical bytes never rewrite, so a retried activation or two
runs producing the same value converge on one file.

Reasons:

- The release requirements fix this record shape; v0.4 diffing and export
  read it unchanged, so the address scheme cannot be revised later without
  breaking the first consumer.
- Canonical serialization makes the hash a function of the value alone. A
  dict whose keys arrive in different orders hashes identically, so equality
  of content-addresses means equality of values, which diff tooling relies
  on.
- Input hashes use the same function over each input field's dump, giving
  every artifact a reproducible link to the exact inputs that produced it:
  recompute the hashes, compare against `input_hashes`, and you know whether
  the same inputs still yield the stored output.

## Sidecar metadata

One JSON file beside each blob (`<sha256>.json`) carries run_id, node_path,
name, input_hashes, sha256, and path.

Reasons:

- The blob address must stay pure content: anything that names the producer
  would make identical outputs from different runs land at different
  addresses, defeating deduplication. Producer identity therefore lives in a
  sidecar instead of the address.
- A sidecar can be overwritten by a later producer of identical content;
  that is accepted rather than fixed because the provenance stream in the
  run file remains the authoritative write log, addressed by
  `artifact_sha256`. The sidecar is a convenience for browsing the store,
  not the record of truth.

## What activates persist

Every successful activation whose class declares `artifacts` persists its
declared fields: wired children from their node functions, the run root from
the completion handler in `_drive`, both through one code path. Artifact
records land before the scope's `ok` node_activation record, so a completed
scope in the stream implies its artifacts are already on disk.

Reasons:

- Composites can declare artifacts exactly like leaves because recursion
  makes them ordinary nodes; special-casing roots separately would have
  meant two persistence paths to keep in agreement.
- Persisting before the ok record costs nothing and lets consumers treat
  `node_activation {status: ok}` as "this scope's outputs are durable".
- An engine constructed without an ArtifactStore skips persistence silently.
  Tests and dry validation never want a project tree; requiring one would
  have made the simplest engine constructor lie about what it does.

## Provenance payload

`artifact_write` records carry exactly `{"artifact_sha256", "name",
"input_hashes"}` per the implementation plan. Human submissions emit their
own `artifact_write` shape (`{"artifact", "artifact_sha256"}`) from Step 9;
the two shapes coexist deliberately, since a submitted review has no
producing-activation input hashes while a persisted output always does.
