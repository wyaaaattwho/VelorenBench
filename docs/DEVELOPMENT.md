# Project-site development

The site is plain HTML/CSS/JavaScript. It has no remote runtime dependencies,
analytics, third-party fonts, build framework or model calls. GitHub Pages can
serve this directory directly; `.nojekyll` disables Jekyll processing.

```sh
# From the repository root:
python3 -B tools/build_site.py
python3 -B tools/build_site.py --check
python3 -B tools/test_site.py
node --check docs/app.js
node --check docs/tasks_data.js
python3 -m http.server 8000 --bind 127.0.0.1 --directory docs
```

Stop the local preview server after reviewing. For GitHub Pages select one
source: **main /docs**, or publish this directory’s tree at the root of
**gh-pages**. Do not enable conflicting publication sources.

## Content contract

- `task_notes.json` is bilingual editorial context, **not task configuration**.
- `tools/build_site.py` combines those notes with unchanged `scenes.json` and
  generated `TASKS.md` requirements. It emits `tasks_data.js` and the marked
  README compendium; `--check` is read-only.
- Navigation goals containing runtime coordinates are fixture-generated. Their
  catalog descriptions are not represented as literal captured opening goals.
- All task/card/filter IDs and terrains come from the authoritative catalog.
  Image support means admitted engineering interface coverage; T05 remains
  image/task-engineering-only.
- `validation.json` is a public metadata snapshot of historical E261. It retains
  original/effective classifications, independent retry identities, terminal-hold
  policies and unknown runtime-model metadata. It is not a new experiment.
- `assets/media-provenance.json` records each screenshot/clip’s source hash,
  task/run association and exact presentation transform. Screenshot compression
  is lossless; MP4 excerpts are lossy and silent. No source screenshot is
  relabelled from another task.
- `tools/package_source.py` inventories the site as well as code. Regenerate
  with `--write` **after** deliberate documentation/media edits, then run
  `--check`. This does not change the immutable local delivery or native
  qualification registry.

## Accessibility and performance

Language selection uses optional local storage and browser-language defaults.
Filters, dialogs and modality tabs work with keyboard input; native dialogs
trap focus and close with Escape. Deep links use `#task=T01`. Search operates in
both languages regardless of selected display language. Clipboard denial has a
manual-copy fallback message.

Gameplay sources declare H.264 explicitly and include VP9/WebM fallback for
browser builds without proprietary codecs. Both formats use the same original
14-second native source windows; encoded media never changes evaluation data.

Motion respects `prefers-reduced-motion`; content remains visible with no
IntersectionObserver support. Ambient particles stop in hidden tabs and use a
bounded draw count/DPR. Task PNGs are lazy-loaded; videos do not autoplay and
use `preload="none"`. System typography works offline. With JavaScript disabled,
the README is linked as the complete illustrated fallback.

Keep screenshots in the source repo small, never replace them with fabricated
art, and preserve upstream licensing/credits. Do not publish raw result journals,
private fixture state, model conversations, local credentials or asset trees.
