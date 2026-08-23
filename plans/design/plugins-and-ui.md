# Plugin system and plugin UI

Decisions with reasons. Scope and release boundaries live in `../releases/v0.5.md`; this doc records why, not what.

## One plugin, any combination of parts

A plugin is one Python package under `ngen-weave.plugins` whose declaration has optional sections: node types, services, workflow packs, API routes, UI widgets. A backend-only (notifications) or frontend-only (custom artifact viewer) plugin is a plugin with empty sections. Rejected alternative: separate "UI plugin" categories — they force artificial twins that drift out of sync.

Plugin API routes mount under `/api/plugins/<plugin-id>/...`. Namespaced by construction, so collisions are impossible, and they inherit the server's auth middleware like every core route; plugin endpoints are never a side door around auth.

## Widget mechanism: specs everywhere, core included

One widget format for everything: an HTML element tree with complete literal Tailwind class strings plus data bindings to HTTP endpoints, rendered by one generic host component in ngen-weave-web. Core's own field renderers use the same spec format through the same host, dogfooding the API the way reference plugins keep the plugin API honest. The host knows the spec contract and nothing about any plugin, preserving the rule that ngen-weave-web is generic by construction.

Why spec-driven rather than components from day one: class strings and fetch targets are inert data, which collapses the loading/sandboxing/versioning problem arbitrary JS raises; agents author it reliably; diffs read cleanly; and most node UI needs less than people expect.

### Tailwind rules (the contract)

- Class strings are complete literals living verbatim in spec files, data-only like workflow config. This is Tailwind's own contract — the compiler greps source for exact strings and emits only those — so it binds core identically to plugins; it is not an extra plugin burden.
- State variation uses full literal alternatives in lookup maps (`{"pass": "border-green-500", ...}`), never string concatenation. Enable-time validation rejects specs whose expected strings cannot be found statically.
- No per-widget stylesheets and no scoped CSS for rendered elements: Svelte scoping needs compile-time components, and spec markup has none. SvelteKit scoping still covers core's hand-written components as usual.
- Class collection runs at install/enable/disable time: scan enabled plugins' specs, emit one safelist stylesheet, cache it. Cost is once per registry change, not per request, so many plugins are not a runtime concern. Regeneration shares the hook that writes capability grants. Browser-side JIT rejected: two rendering paths give every bug two hiding places, for a benefit only needed if the static-string rule fails, which validation mostly prevents.
- Plugin CSS is not policed. A malicious class string executes nothing, and plugins already run arbitrary Python under "sandboxed by convention", so scanning for `fixed inset-0` would be inconsistent theater. Widgets render in a constrained container (clipped, z-index bounded) as hygiene. The remedy for a badly behaved plugin is operational: `ngw plugins disable` / `remove`.

### Deliberate limits

- Bindings poll only (`poll_ms`). The product targets locally hosted deployment, where polling costs nothing. Streaming bindings wait.
- The binding/action vocabulary stays tiny: fetch-and-update, nothing else. Conditionals, loops, transforms, client state — growing these turns JSON into a bad programming language. Anything genuinely interactive goes through the component-bundle path below.

### Costs accepted on purpose

Spec elements get none of Svelte's compile-time guarantees or fine-grained reactivity. Owned mitigations, both v0.5 obligations: enable-time validation (spec tree schema, route references resolve within the same plugin) and per-widget error boundaries so one bad plugin cannot take down the canvas.

## Component bundles pulled forward

The post-1.0 escape hatch ships early: a plugin may include prebuilt frontend component bundles compiled at authoring time against the published SDK (`ngw plugin build`), loaded generically into declared, stably named mount slots. Pip still distributes one artifact; the web app never rebuilds and never sees source.

Rejected: labeling plugins "svelte-native" and pulling them into the host's build cycle, even optionally. It makes every plugin install a frontend build event, breaks prebuilt web-app distribution, couples compiled plugins to host internals at their build time, and forks every authoring decision into "spec or component?". Authoring ergonomics live in the SDK + `ngw plugin build`, not in host-build coupling. Mount points are named and stable now because they are the seams these bundles plug into.

## Open questions

| question | blocks | notes |
|---|---|---|
| none currently | | |
