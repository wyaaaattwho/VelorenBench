# Native corresponding source

Upstream project: **Veloren**, <https://gitlab.com/veloren/veloren>,
<https://veloren.net/>.

The inherited lineage HEAD is
`f974743d2d6573ae8db7bb3f674212fb3cc08ad5`. The selected benchmark source is
a modified content-inventoried tree, **not an unmodified upstream checkout
or a known clean Git commit**. Original source headers, workspace/package
metadata, Cargo.lock and GPL LICENSE are preserved. The local modifications
implement the native agent bridge, private fixture/oracle instrumentation,
ordinary renderer frame/action interface, native RPG interactions and
corresponding server-side outcome evidence. They do not relicense Veloren.

`source.json` retains the full selected inventory, lineage and patch
identities. No `.git` history, assets or dependency/compiler caches are
bundled. One compile-time sound resource is intentionally omitted from
`source/`; see [BUILD.md](../BUILD.md) for externally supplying and verifying
it and [PROVENANCE.md](../PROVENANCE.md) for distinct selected/export identities.

The Python code under `source/agent_controller/` is the inherited low-level
protocol client and corresponding interface documentation/tests, not a
private task-qualification controller. It remains within this GPL native
component and is not part of the MIT Python distribution.
