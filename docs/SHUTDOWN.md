# Window shutdown / background cancellation (#525)

Closing the window sets `EagleBrowseWindow._shutdown` **before** timers and
workers are torn down. Background jobs check that event and must not queue UI
work afterward (`_ui_idle` / `wrap_idle_callback`).

## Write / import policy

| Operation | On shutdown |
|-----------|-------------|
| Inbox import | Abort **between** items; finish the current `import_file` / write. Skip duplicate-review UI; leave remaining inbox files. |
| Duration backfill | Stop further probes; still write the short batch already probed. Skip UI refresh. |
| Trim / save-frame | Do not start new work. In-flight ffmpeg/write may finish; UI upsert/toast is skipped via `_ui_idle`. |
| Staging | Abort between copies. |
| Integrations (HTTP) | Let remote POST finish; skip toasts / library tag writes after shutdown. |
| Query / scans / counts | Cooperative abort; no UI publish. |
| Thumbs / inspector | Decode may finish or die with the pool; textures are not applied after shutdown. |

## Manual verification matrix

| Scenario | Expect |
|----------|--------|
| Close during smart-folder query | Process exits; no late grid fill |
| Close during duration backfill | Exits promptly; duration JSON only for completed probes |
| Close during manual import mid-batch | Finished items in library; unfinished stay in inbox; no zombie |
| Close during trim / save-frame | Process exits; no GTK toast after destroy |
| Close during upscale / bust / edit | Process exits; at most remote queue left |
| Close during stage / images scan | Exits; scan does not re-arm |
| Super+W while thumbs decoding | No lingering non-daemon process |

Automated coverage: `tests/test_shutdown_gate.py`, `tests/test_latest_job.py`,
`tests/test_duration_backfill.py::test_cancelled_stops_further_probes`,
`tests/test_library_query_cancel.py`.
