# Implementation plans

Empty for now. Naming: `<version> - <feature> - implementation plan.md`, one file per feature, written when its version starts. Each plan assumes the release requirements in ../releases/ and the technical design in ../design/, and breaks work into ordered steps that leave the repo green and committed. See archive/v0.1-ts-yaml/0.md for the step-plan conventions worth keeping.

## Conventions all versions inherit

Every version's plans follow these without restating them.

**Docstrings under defs.** Documentation lives where you read it: every public class/function carries a docstring saying what it achieves, and a module opens with at most two lines naming its job. No file-top indexes of contents; if an agent or human wants to know what a class does, they jump to the class. It is documentation, not navigation: no rationale, no history, no usage examples, nothing that duplicates the PRD or design docs. Updated in the same step as any change to the thing it documents. When a plan lists a new file, its one-line purpose is enough for the executor to write both the header and the def docstrings; the plan does not transcribe their text (exception: files whose wording is itself a deliverable, e.g. a wire-types module pointed at by other docs).

Enforcement: a lint check asserting module headers stay within two lines is deferred until post-1.0 (`implementation/post-1.0.md`); before then it holds by review.
