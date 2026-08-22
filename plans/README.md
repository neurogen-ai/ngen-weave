# Plans

Documentation hierarchy:

```
plans/
├── product/PRD.md        # the product, v0.1 → 1.2+, authoritative on conflict
├── releases/             # per-version release requirements
├── design/               # technical design: system architecture, per-module docs
└── implementation/       # per-feature step-by-step plans (written when a version starts)
```

`archive/` holds superseded TS/YAML-era plans (kept for intent, void as instructions): `v0.1-ts-yaml`, `v0.2-ts-exporter`, `v0.3-read-ui-ts`.

Read `product/PRD.md` first. It carries the decision log; the release docs describe what each version must deliver.
