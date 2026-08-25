# docs

Draft documentation for implemented behavior that the plans do not spell out.
The plans under `plans/` say what each version delivers and why; these pages
record how the shipped code actually works, at the level someone needs to
modify it safely.

Status: drafts. The MkDocs Material site starts at v0.3; until then this
directory is the only prose documentation. When a topic here graduates into a
formal design doc under `plans/design/`, the draft here becomes user-facing
documentation or is deleted.

Contents:

- [cli.md](cli.md): the `ngen-weave` commands, how discovery builds the
  workflow namespace, and where runs and models live on disk.
- [engine/execution.md](engine/execution.md): how Engine.compile turns a
  workflow class into a runnable LangGraph graph, how inputs are assembled,
  how fan-in joins stay correct, and how runs are recorded and resumed.
