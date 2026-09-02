# Changelog

All notable changes to Eagle Browse are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/progressions/eagle-browse/compare/v0.1.4...HEAD
[0.1.4]: https://github.com/progressions/eagle-browse/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/progressions/eagle-browse/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/progressions/eagle-browse/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/progressions/eagle-browse/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/progressions/eagle-browse/releases/tag/v0.1.0
