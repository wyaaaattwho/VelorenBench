# Optional benchmark visualization

The installed **dependency-free Python core** includes a read-only browser viewer,
original-PNG screenshots, optional passive renderer capture, and genuine
H.264/MP4 export. Benchmarking does
not start the viewer or require ffmpeg. No game server, display, control owner,
second camera observer, provider/model call, or private judge reader is started by this
utility. It has no gameplay-control endpoints.

## Installed usage

Use absolute paths. `--source` is the **exact `--recordings` directory of one
renderer benchmark run**, not its private run-result directory. A normal renderer
run publishes `public-view/run.json`, immutable `public-view/frames/*.json`, and
`public-view/latest.json` beside its original PNGs. This feed contains only public
frame attachment descriptors and run/task identity; no scores, private receipts,
model text, credentials or private world state. Existing pre-feed recordings
cannot be auto-discovered or reconstructed from private journals. After
same-player renderer readiness, normal runs also register immutable
`public-view/capture.json`, even in structured mode with **zero image requests**.

```sh
# --port 0 selects a free loopback port and prints its URL. No public bind option.
rpg-benchmark visualize serve --source /data/my-renderer-frames \
  --output-root /data/recordings/my-run --live

# Passive livestream of the existing registered same-run Linux renderer.
# No native frame/UI requests, fence changes, or second camera.
rpg-benchmark visualize serve --source /data/my-renderer-frames --passive \
  --capture-out /data/recordings/my-run/passive-archive --capture-fps 10 \
  --output-root /data/recordings/my-run/passive-exports

# Saved-frame viewing; no freshness requirement or live claim.
rpg-benchmark visualize serve --source /data/my-renderer-frames \
  --output-root /data/recordings/saved-review

# New export directories only; existing paths are an explicit collision error.
rpg-benchmark visualize screenshot --source /data/my-renderer-frames \
  --out /data/recordings/my-run/screenshot-001
rpg-benchmark visualize export --source /data/my-renderer-frames \
  --out /data/recordings/my-run/clip-001 --fps 30

# Optional live source window (this never starts/stops the benchmark).
rpg-benchmark visualize record --source /data/my-renderer-frames \
  --out /data/recordings/my-run/live-001 --seconds 5

# Passive capture + MP4, independent of agent image requests.
rpg-benchmark visualize record --source /data/my-renderer-frames --passive \
  --capture-out /data/recordings/my-run/passive-archive-002 --capture-fps 10 \
  --out /data/recordings/my-run/passive-clip-002 --seconds 5

# Fresh passive PNG, with retained source capture archive/provenance.
rpg-benchmark visualize screenshot --source /data/my-renderer-frames --passive \
  --capture-out /data/recordings/my-run/passive-archive-003 \
  --out /data/recordings/my-run/passive-shot-003
```

Browser actions: browse paginated saved frames, export the **displayed** PNG,
start/stop live source recording, download validated MP4 and provenance JSON.
The default selected source is fixed by the CLI: there is no filesystem browser,
automatic newest-run selection or another observer's camera. PNG download is
byte-identical to the source, not a JPEG screenshot of browser decorations.
`screenshot --frame KEY` selects a receipt key from `public-view/frames/KEY.json`.

Programmatic seams:
`frame_feed.Source(absolute_directory, live=False)`,
`visualization_media.screenshot(source, new_directory, row=None)` and
`visualization_media.movie(source, new_directory, fps=30)`.
`Source.catalog()` returns capture-time-ordered public metadata (not pixels).
`passive_capture.PassiveSource(source_directory, new_archive_directory, fps=10)`
implements the same source interface for optional X11 capture; call `wait_ready()`
before exporting and `close()` in a `finally` block.

## Source versus live recording

There are two explicitly selected source modes, **not two viewers**:

* Default agent-frame source: only successful public `frame` responses already
  requested by the agent are published. This retains native frame IDs and exact
  original PNGs. No requests means empty/stale; sparse observations are not a
  continuous livestream.
* `--passive`: ffmpeg X11 input samples the **existing registered renderer's
  display**, independently of observation mode or image requests. Normal-run
  registration attributes run/task/session/actor from verified same-client
  renderer readiness, plus boot ID, current-user PID/start/executable/argv
  identities, actual X11 listener/socket ownership and geometry. These are pinned
  and rechecked. A changed receipt, socket/display, PID generation, geometry or
  stopped/dead source is refused, with no automatic reattachment/fallback.
  No native frame/UI/control socket is opened; agent frame/UI fences are untouched.

Passive capture requires an explicit **NEW `--capture-out` archive directory**.
It records source PNGs/metadata to disk while enabled, including outside MP4
start/stop windows, so saved browsing/export uses the same source interface.
Disk grows with duration/geometry/fps: choose `--capture-fps` (1..30, default 10)
and stop capture when done. This explicit source archive is not an in-memory
viewer queue; ordinary benchmarking never starts it. Only the utility's own
ffmpeg child is retired on exit, not the registered renderer or Xvfb.
The archive can later be selected with ordinary `--source /data/.../passive-archive`
without `--passive`. Saved PNG viewing/export requires neither ffmpeg nor a live
renderer. No private journal reconstruction or second camera is used.

* `export` reads all selected run's saved public receipts. Ended/stale sources
  are valid **saved** media, explicitly recorded as such.
* Live recording start includes the current frame; stop selects all persisted
  receipts through the current frame, including intermediate frames that the
  preview may have skipped. It does not pad the clip to the button/wall-clock
  interval. Both boundaries must be fresh and running; stale, stopped, errored
  and empty feeds report a failure. Interrupted start-without-stop is not a clip.
* Native frames use actual Unix-realtime `capture_host.begin_ns` differences.
  Passive samples use actual ffmpeg X11 wallclock PTS, rescaled to microseconds
  and stored as `capture_host.sample_pts_ns`, with separate `received_ns`.
  These do **not** claim native readback timestamps. Timestamp differences
  determine frame holds.
  Concat input uses a millisecond timebase; output uses CFR `--fps` resampling,
  holding prior pixels, not interpolating or fabricating new source captures.
  The last frame is held for `1/fps` second. A single-frame clip is that hold only.
  Container duration/decoded frame count may be rounded to the output timebase.
  Distinct capture origins/sessions cannot be silently mixed into one movie.
* Missing, non-increasing or mixed session/actor/resolution timestamps/identities
  are errors, not guessed timing. Local client update counters are not server
  ticks. Capture ordering does not prove an action's effects or task success.

`metadata.json` retains run/task/session/actor/frame IDs, every included original
PNG hash/filename/dimensions/capture times, source state, transformation, timing
policy, output hash, encoder command and probe. MP4 is lossy libx264/yuv420p;
odd dimensions get black padding to even dimensions. PNG retains original bytes.
Keep original source frames and metadata if exact pixels are needed.

Passive provenance is explicit: `capture_origin.kind=passive-renderer-x11`,
capture session/sequence, source receipt hash/display, and independent
`frame_id_scope=passive-capture-session-sequence`. `native_frame_id` is **null**;
no UI-state ID or actuator fence is fabricated. Actor/session attribution is
from the pinned readiness registration, not a per-image native receipt.
Geometry, ownership and encoder command are retained in archive `run.json`.
Unchanged pixels while the agent thinks are real repeated passive samples,
not evidence that gameplay advanced. Freshness uses X11 PTS, not publication
time, so buffered/frozen captures cannot masquerade as fresh.

## Encoders, errors and safety

MP4 requires system **ffmpeg and ffprobe with libx264** on `PATH`; they are
preflighted before recording/export. There are no Python media dependencies and
no silent WebM/GIF substitution. Passive preview/PNG additionally requires Linux
procfs, existing X11/ffmpeg PNG input/output support and `xdpyinfo`; missing tools
or unregistered/non-owned sources fail explicitly. No automatic substitution is
performed on other platforms. Successful MP4 export requires positive decoded
frame count, an actual MP4/H.264 probe, and a full ffmpeg decode. Encoder failures
leave explicit error metadata/logs and possibly `video.partial.mp4`, never a
success-labelled empty movie. The encoder child is owned/retired independently;
registered game/renderer/other-recorder processes are never signalled. Passive
capture reads only registered process identity/ownership and the DISPLAY field,
not private judge state or retained credentials.

Passive shutdown has one pipe owner: the capture worker stops publishing, sends
SIGTERM to its own encoder, and drains both output pipes before reaping/closing
them. EOF does not imply process exit; the child retains the same graceful-stop
deadline after closing its pipes. Shutdown drain bytes are discarded, never
published as new frames. `capture_encoder_cleanup` records exit code, EOF/close
state, `kill_attempted`, and `forced_termination` (an attempted kill followed by
an actual SIGKILL exit, not a terminal exit that won the poll/kill race).
Forced termination, incomplete drain, or cleanup failure marks the capture
archive as error; earlier capture errors are retained. Complete saved PNGs remain
available through the ordinary saved-source export path. The shutdown regression
uses an owned X11 painter/real encoder, not gameplay or release-acceptance proof;
it does not establish the exact cause of older encoder failures.

Each export reserves a new directory. No overwrite, arbitrary download path,
shell interpolation or inherited fixed port/display/root exists. Output roots
must not overlap the read-only source. Loopback Host checks, same-origin Origin
checks and session-token CSRF protect actions; downloads use server-registered
identities. Symlink/traversal source paths and nonregular/oversized files are
refused. At most eight HTTP clients are served concurrently, with socket timeouts.
Preview uses one shared latest validated PNG/condition, not per-viewer frame
queues. The benchmark writes only small public metadata: it never waits for
viewers/encoding. Saved catalog/MP4 export holds metadata in memory and spools one
PNG at a time to disk; it can require substantial disk space/encoding time for a
long run. There is no background automatic export.

## Reuse and integration provenance

Adapted from the prior experiment's `veloren_observer_dashboard.py`, `.html`
and `.js`: latest-frame/condition fanout, bounded streaming/disconnect handling,
sequential browser polling, truthful stale state, recording start/stop/probe
lifecycle, Host/Origin/token defenses and registered downloads. The original
observer Camera PID/start/socket attachment checks and X11 Preview encoder-pipe/
owned-child lifecycle are also adapted in `passive_capture.py`. Its camera-only
receipt/control-root topology is replaced by normal-run same-player registration;
no camera process or WebM path is inherited. The projection is not needed.
No original livestream files or recordings were modified.

The normal-run hook is `RendererBoundary.public_response` ->
`frame_feed.Publisher.publish` on successful public `frame` responses only.
`run.py` supplies public run/task identity. After same-player readiness,
`RendererBridge.start` registers its already-owned dynamic display/processes;
capture never starts automatically. `RendererBridge.close` marks the feed ended
before retiring its processes. Publication/registration errors are explicit
media diagnostics and do not mutate the
agent's public response. A crashed runner can leave `running` metadata, but frame
capture age still makes the live feed stale; `running` is not a PID/liveness proof.
No native Rust modification is required.

Offline frame and owned-changing-X11-display fixtures test plumbing/pixels/MP4
and no-frame-request independence. Actual-native integration still needs a normal
installed renderer run, verification of the registration/identity/end lifecycle,
and export of its real pixels (agent frames or explicit passive capture).
Fixture media is engineering evidence, **not actual benchmark/gameplay video**.
