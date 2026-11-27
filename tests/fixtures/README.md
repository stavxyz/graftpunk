# Test fixtures

## `session_compat/`

Saved sessions written by released graftpunk versions, made by
`scripts/session_compat.py`, and the browser-free load fixture in
`session_compat/legacy/`. See
[`session_compat/README.md`](session_compat/README.md) for what each holds and
how it was made. Every file there is synthetic test data with a throwaway key.

## `selenium_late_post_perflog.json`

The Network events of a live Selenium login (Chrome 158, a local http site, on
2026-11-27) whose POST's `requestWillBeSentExtraInfo` arrived after its redirect
GET was announced, trimmed by `scripts/trim_selenium_perflog.py`: header values
are placeholders except the ones the correlator and the classifier read, and no
request body is kept. `tests/unit/test_observe_headers.py` replays it through
`SeleniumCaptureBackend` to check the POST and the GET keep their own sent
headers.
