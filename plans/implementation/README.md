# Implementation plans

Empty for now. Naming: `<version> - <feature> - implementation plan.md`, one file per feature, written when its version starts. Each plan assumes the release requirements in ../releases/ and the technical design in ../design/, and breaks work into ordered steps that leave the repo green and committed. See archive/v0.1-ts-yaml/0.md for the step-plan conventions worth keeping.

## Conventions all versions inherit

Every version's plans follow these without restating them.

**Module docstrings.** Every source file opens with a semantic module docstring, ≤20 lines: one sentence on what the module achieves, then one entry per public class/function — name plus what it achieves. It exists so agents and humans can query the top of a file instead of reading it whole. It is a navigation aid, not documentation: no rationale, no history, no usage examples, nothing that duplicates the PRD or design docs. Updated in the same step as any change to the file's public surface. When a plan lists a new file, its one-line purpose is enough for the executor to write the docstring; the plan does not transcribe header text (exception: files whose wording is itself a deliverable, e.g. a wire-types module pointed at by other docs).

Enforcement: a lint check asserting the docstring exists and stays under the limit is deferred until post-1.0 (`implementation/post-1.0.md`); before then it holds by review.
