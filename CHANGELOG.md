# Changelog

All notable changes to Eagle Browse are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.14] — 2026-10-01

### Changed

- Batch metadata edits update the shared modification index once per batch and skip unchanged assets while preserving backups and atomic saves. In a disk-backed synthetic benchmark, assigning a category to 100 assets improved from a median of 4.77 seconds to 1.71 seconds (PR #45).
- Category, tag, rating, note, and group membership edits run in the background with progress. Accepted writes finish safely if the picker or window closes.

### Fixed

- Category and tag pickers report batch errors and reconcile mixed membership after partial failures instead of indicating full success.

## [0.1.13] — 2026-10-01

### Fixed

- Remember the Collapse groups toggle across restarts, saving both On and Off immediately. Existing installations default to Off until enabled (PR #43).

## [0.1.12] — 2026-10-01

### Changed

- Tag and folder edits on collapsed group thumbnails apply to every non-deleted member. Mixed selections deduplicate assets; inspector and editor values reflect shared and mixed metadata across all targets.

### Fixed

- Esc closes asset and group layers in order: video → group → originating folder or library view. The group parent is independent of Back history, preventing Esc from reopening a video.

## [0.1.11] — 2026-10-01

### Fixed

- Esc in a group grid now returns to the previous view using the same action as the Back button, before clearing selection or search. Open dialogs and individual asset viewers still close first.

## [0.1.10] — 2026-10-01

### Added

- Collapse groups toggle beside Sort displays one thumbnail per set after filtering and sorting. Opening the representative thumbnail shows all set members; turning the toggle off restores individual assets.

## [0.1.9] — 2026-09-25

### Changed

- Enhance bust opens with Qwen checked. Flux Klein and Krea 2 remain available.

## [0.1.8] — 2026-09-25

### Changed

- Edit dialog no longer shows the paragraph about other selected stills and engine behavior. The prompt box and engine checkboxes are unchanged.

## [0.1.7] — 2026-09-25

### Changed

- Edit (`u` then `e`) keeps posting `engine=qwen` (PromptForge maps that to Qwen Image 2.1). Other selected stills are sent as `ref_image_eagle_ids` for Qwen only, becoming `<image2>`…. Flux and Krea remain single-image edits. One focused still with no extras keeps the previous payload. Flat-lay now names the Qwen 2.1 workflow that PromptForge runs; bust, wardrobe, spicy, and flat-lay do not send extra refs ([#577](https://app.fizzy.do/6109848/cards/577)).

## [0.1.6] — 2026-09-07

### Added

- Opt-in `inbox_subfolders_as_categories` configuration (default false): recursively import intake media into the category named by its first subfolder. Category matching is case-insensitive, and new names are lowercase ([#551](https://app.fizzy.do/6109848/cards/551) / PR #31).

### Fixed

- Track intake readiness and duplicate-review signals by relative path so identical filenames in different subfolders remain independent.
- Refresh newly created categories in the browser sidebar and reload reused items so category membership, filtered views, and counts update immediately.

## [0.1.5] — 2026-09-04

### Added

- Shift+E sends every selected video/audio to the current Clip Editor project; Ctrl+Shift+E starts a new project with the whole selection ([#539](https://app.fizzy.do/6109848/cards/539) / PR #29). Requires Clip Editor with repeated `--video`/`--audio` (#540).

## [0.1.4] — 2026-09-02

### Added

- Multi-engine toggles on Edit, Add wardrobe, and Enhance bust: select Qwen / Flux / Krea independently and queue one PromptForge job per engine ([#512](https://app.fizzy.do/6109848/cards/512) / PR #26).
- Coordinated window shutdown: background workers check a shutdown signal and skip UI callbacks after close ([#525](https://app.fizzy.do/6109848/cards/525) / PR #25). See `docs/SHUTDOWN.md`.
- Lifecycle / concurrency / performance regression tests, plus a synthetic catalog fixture (`synth_catalog.py`) and README Testing section ([#526](https://app.fizzy.do/6109848/cards/526) / PR #24).
- Async inspector previews that reuse grid thumbnails when resolution is sufficient ([#523](https://app.fizzy.do/6109848/cards/523) / PR #23).
- Byte-bounded LRU thumbnail cache with zoom-size reclaim and debug metrics ([#522](https://app.fizzy.do/6109848/cards/522) / PR #21).
- Ctrl+Enter submits Edit / Flat-lay dialogs (plain Enter still inserts a newline) ([#505](https://app.fizzy.do/6109848/cards/505) / PR #22).

### Changed

- Flat-lay remains Qwen + QIE-2511 only (recipe lock); other edit surfaces support multi-engine submit.

### Fixed

- Package `py-modules` now includes `thumb_cache` and `shutdown_gate` so installed builds match source imports.

### Notes

- Spicy variations are not a Browse surface yet; multi-engine toggles cover Edit / wardrobe / bust only (#512).

## [0.1.3] — 2026-09-02

### Removed

- LAN phone-browse stack (`phone_server`, `phone-browse`, phone web UI, systemd unit) ([#519](https://app.fizzy.do/6109848/cards/519) / PR #19). Use the dedicated phone/LTE viewer instead.

### Fixed

- Duration backfill probes media outside the library write lock; short locked write batches with revalidation, backoff, and durable skip ([#518](https://app.fizzy.do/6109848/cards/518) / PR #18).

## [0.1.2] — 2026-09-02

### Fixed

- Query-cache invalidation is race-safe: synchronized clears plus a generation guard so stale in-flight queries cannot republish ([#517](https://app.fizzy.do/6109848/cards/517) / PR #16).

### Added

- U-menu Flat-lay: worn still → wardrobe sheet via PromptForge QIE recipe ([#510](https://app.fizzy.do/6109848/cards/510) / PR #15).
- Serialized, cancellable library queries (PR #13).

## [0.1.1] — 2026-08 / 2026-09

### Added

- Integrations menu (upscale / bust / wardrobe) and keyboard flow (`u` then letter) ([#477](https://app.fizzy.do/6109848/cards/477), [#478](https://app.fizzy.do/6109848/cards/478)).
- U-menu Edit → PromptForge ([#503](https://app.fizzy.do/6109848/cards/503)).
- Make more in PromptForge ([#483](https://app.fizzy.do/6109848/cards/483)).
- Stamp `pf:<id>` from `image-<id>-` filenames on import + backfill ([#482](https://app.fizzy.do/6109848/cards/482)).

## [0.1.0] — 2026-08

- Initial packaged release of the keyboard-first GTK Eagle.cool browser and inbox watcher.

[Unreleased]: https://github.com/progressions/eagle-browse/compare/v0.1.14...HEAD
[0.1.14]: https://github.com/progressions/eagle-browse/compare/v0.1.13...v0.1.14
[0.1.13]: https://github.com/progressions/eagle-browse/compare/v0.1.12...v0.1.13
[0.1.5]: https://github.com/progressions/eagle-browse/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/progressions/eagle-browse/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/progressions/eagle-browse/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/progressions/eagle-browse/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/progressions/eagle-browse/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/progressions/eagle-browse/releases/tag/v0.1.0
