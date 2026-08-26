# Constants

Cross-module tunable constants live in dedicated `constants.py` modules: `ngen_weave.constants` for the core library, `ngen_weave_cli.constants` for the CLI (placeholder until it has its first entry). They exist so a tunable is never defined floating in a consuming module, where import cycles decide who owns it and reviewers hunt for values that moved.

What belongs there: module-level numbers and strings shared across modules or tuned independently of code logic (`REPLY_EXCERPT_CHARS` today). What does not: user-facing settings — those live in `config.py` / `models.json` where authors set them — and private helpers like the engine's `_INPUT_KEY`, which stay local to their module because nothing else may touch them.

Rule: any new cross-module tunable goes in `constants.py` with a one-line comment saying what it controls. If a constant has exactly one consumer and no tuning story, leave it where it is used.

Current contents:

| name | value | purpose |
|---|---|---|
| `REPLY_EXCERPT_CHARS` | `500` | how much of the last model reply rides an exhausted `AgentReplyError` message |
