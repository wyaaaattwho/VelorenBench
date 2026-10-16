# License and attribution boundaries

This repository is a source release of independently scoped components,
not a blanket license for Veloren, its assets or external providers.

| Component | Terms and boundary |
| --- | --- |
| Researcher-owned original Harness, tools and documentation | MIT, authorized for this release |
| Adapted GameAgent Harness/adapter code | Existing MIT attribution: Copyright (c) 2026 VictorYXL; full notice in root LICENSE |
| Original `integrations/deepseek/` adapter, binding tools and tests | MIT; separate installable distribution, not the official DSH runtime |
| `native/source/` modified Veloren and corresponding bridge code | GPL-3.0-or-later as declared by the workspace; original GPL text retained at `native/source/LICENSE` |
| Rust/Python dependencies, toolchains and system libraries | Their own upstream terms; downloaded externally, not bundled or relicensed |
| Veloren assets | External only; per-file credits and licenses apply independently |
| Optional SDK, provider service and user credential loader | Separately obtained and authorized; not supplied by this repository |

The root MIT text retains the evidenced GameAgent attribution; it does not
invent an additional copyright holder. MIT applies to the original Harness
and adaptations covered by that license, **not the GPL native subtree**.
The core Python wheel contains the MIT Harness and supporting metadata. The
source distribution also carries its tests/tools and the original DeepSeek
integration source; the latter remains a separately installed package.
Neither contains native source, game assets, the official DSH runtime or
third-party code caches. The Git checkout separately carries native
corresponding source. Official npm dependencies retain their own terms.

Veloren is developed by the Veloren contributors:
<https://veloren.net/> / <https://gitlab.com/veloren/veloren>.
Native lineage and local modifications are identified in
[native/UPSTREAM.md](native/UPSTREAM.md) and [PROVENANCE.md](PROVENANCE.md).
Existing source notices and Cargo metadata remain intact. The low-level
native agent bridge is corresponding interface source, not a private
qualification solver.

No game assets are included, including the compile-time `null.ogg` resource.
The build tool requires its exact externally supplied bytes. The expected
asset inventory provides identity, **not permission**. Obtain assets from a
lawful source, preserve their credits and check terms before redistribution
or publication of recordings. Veloren asset credits include mixed free and
restricted terms; do not assume the engine's GPL or the Harness's MIT covers
them. This repository does not resolve missing/ambiguous asset attribution.

No binaries, dependency sources, compiler caches, worlds, historical run
data, private gameplay controllers, credentials or optional GameAgent runtime
are distributed. Public-service URLs in protocol/provider source are API
definitions, not credentials or private endpoints. Before distributing
locally built binaries, satisfy GPL corresponding-source and dependency/
asset obligations for that actual build; this source release is not a legal
compliance opinion for arbitrary derived bundles.
