# Native gameplay media — attribution and scope

The PNGs in `tasks/` and silent MP4s in `videos/` depict **Veloren**, developed
by the **Veloren contributors**:

- Project: <https://veloren.net/>
- Upstream source and assets: <https://gitlab.com/veloren/veloren>
- Exact native source lineage: [native/UPSTREAM.md](https://github.com/wyaaaattwho/VelorenBench/blob/main/native/UPSTREAM.md)
- Modified corresponding native source and GPL-3.0-or-later text:
  [native/source/](https://github.com/wyaaaattwho/VelorenBench/tree/main/native/source)
- Local copy of the upstream GPL text: [GPL-3.0.txt](GPL-3.0.txt).
- Snapshot of upstream asset attributions: [veloren-credits.ron](veloren-credits.ron).
  Original authors, per-resource licenses and exceptions are retained there.

**Rendered game art is not relicensed as MIT.** The harness/documentation MIT
license does not replace upstream engine, art or font terms. The media are
non-commercial benchmark documentation and gameplay illustrations. No raw
Veloren asset tree, music track or font binary is distributed by this site;
all excerpts are silent. Upstream credits include GPL, Creative Commons,
font-specific terms and separately stated gameplay-image/video exceptions.
Preserve the source credits and applicable terms when reusing these images;
do not assume the repository’s root license covers all depicted content.

Default attribution in the pinned upstream `credits.ron` assumes GPL3 where no
other asset license is given. Named visual resources include fonts by their
credited creators and game art by the Veloren contributors. The complete pinned
credit snapshot, rather than a guessed new artist list, is retained alongside
these images. Restricted per-file art is not separately redistributed.

## Transformations and evidence identity

[`media-provenance.json`](media-provenance.json) lists every published file,
original source hash, task/terrain association, source receipt reference and
presentation transformation:

- Task images: native 960×540 PNGs, lossless recompression, unchanged decoded
  pixels. Some are archived qualification/E261 frames; others are bounded,
  no-model `game_contract` + `game_frame` captures.
- Gameplay clips: 14-second real-time windows from E261 renderer videos,
  H.264/yuv420p MP4 with VP9/WebM fallback renditions, resized to768×432 at20fps,
  audio removed. Both renditions independently use the same original source
  window (no cascading transcode). No interpolation,
  fabricated action, artificial speedup or terminal-hold segment is used.
- Hero: the same N7 screenshot, displayed with CSS cropping and color/gradient
  overlays. The website’s ambient particles are decorative UI, not game footage.

Screenshots are **environment illustrations**, not independent evidence of
task success. The immutable original archives retain their bytes. These
presentation transforms do not rewrite benchmark results, qualification,
model metadata or the sealed distribution identities.

The rune favicon and site design are original documentation art, not a copied
Veloren logo. VelorenBench is independent and does not imply upstream endorsement.
The original repository MIT notice is also retained at [../LICENSE](../LICENSE)
for documentation/site code; it does not relicense the depicted game content.
