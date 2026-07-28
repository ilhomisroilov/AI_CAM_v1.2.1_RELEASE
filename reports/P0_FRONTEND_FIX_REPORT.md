# P0 — Frontend Blocker: Root Cause & Fix

**Status: FRONTEND VALIDATION: PASS** (real browser, visible page).

The frontend WAS genuinely broken — this corrects an earlier report that wrongly
called it "already correct". That earlier check used the in-app browser pane which
runs **hidden** (`document.visibilityState = "hidden"`); the poller correctly
pauses when hidden, which *masked* the real defect. A visible page is required to
see it — exactly why real-browser execution is mandatory here.

## Reproduce (real browser, Playwright)

`python run.py` → headless Chromium (visible) → login admin/admin → `/dashboard`.
Captured **before** the fix:

```
dashboard.visibility : "visible"
dashboard.api_req    : []            # NO /api/status, /api/plc/status, /api/rfid/status, /video_feed
tCam / tAi / tPlc    : "—"           # cards stuck
history.records_req  : false         # NO /api/records
page_errors          : ["Illegal invocation", "Illegal invocation", "Illegal invocation"]
console_errors       : []
polling_defined,dash_owner : true    # the poller IS created
```

## Root cause — `frontend/static/js/polling.js`

The poll owner stored the native timer functions as instance properties and then
called them as methods:

```js
this.setIntervalFn = options.setIntervalFn || root.setInterval;   // = window.setInterval
...
this.timer = this.setIntervalFn(fn, ms);   // called with this === PollOwner
```

`window.setInterval` requires its receiver to be `window`; invoking it with
`this === PollOwner` makes the browser throw **"Illegal invocation"**. That throw
happens inside `_arm()`, which `start()` calls **before** `runNow()` — so startup
dies and the **initial request never fires**, and no interval is ever armed.

Why it was invisible until now:
* `_arm()` returns early when the page is hidden, so the broken `setInterval`
  line is never reached on a hidden page → the in-app hidden pane never threw.
* The unit harness (`tests/frontend_polling_harness.js`) **injects fake** timer
  functions, so the native-`this` binding is never exercised → it stayed green.

Both dashboard and history use the same `polling.js`, so one defect broke both.

## Fix

Bind the native fallbacks to `window` (injected test doubles are still used as-is):

```js
this.setIntervalFn  = options.setIntervalFn  || root.setInterval.bind(root);
this.clearIntervalFn = options.clearIntervalFn || root.clearInterval.bind(root);
```

Also hardened `history.js`: all top-level `addEventListener` wirings are now
null-safe (`?.`) so a template/element mismatch (v1.1.3↔v1.2.1 drift) can no
longer throw before the records poller is created.

## After the fix (same real-browser run)

```
dashboard.visibility : "visible"
dashboard.api_req    : ["/api/plc/status", "/api/rfid/status", "/api/status"]
dashboard.api_resp   : all 200
tCam="Offline"  tAi="Ready"  tPlc="IDLE (0)"  tRfid="Off"   # cards populated
history.records_req  : true   status 200   rows 1
page_errors : []   console_errors : []
```

`/video_feed` is requested **only when the camera is connected** (correct
standby design); with no camera it shows the placeholder rather than opening a
dead stream.

## P0.3 — version/host audit

No `v1.1.3`, `localhost`, `127.0.0.1`, `:8000`, `/api/v1/`, or hardcoded host in
the frontend; every API call is same-origin relative (`/api/status`,
`/api/plc/status`, `/api/rfid/status`, `/video_feed`, `/api/records`, `/health`).

## Tests (P0.8)

* `tests/test_frontend_polling.py` (mock state-machine harness) — still passes.
* `tests/test_frontend_playwright.py` — **new real-browser acceptance test**:
  starts the server, logs in, asserts the three status requests fire + return
  200, the cards leave the `—` placeholder, `/api/records` fires + renders, and
  **zero** console/page errors. This test fails on the pre-fix code and passes
  after. Full suite: **378 passed, 4 skipped**.

## P0.9 — acceptance gate

| Gate | Result |
|------|--------|
| Console errors = 0 | PASS |
| `GET /api/status` 200 | PASS |
| `GET /api/plc/status` 200 | PASS |
| `GET /api/rfid/status` 200 | PASS |
| `GET /video_feed` | Requested when camera connected (placeholder standby otherwise) |
| `GET /api/records` 200 | PASS |
| Camera/PLC/RFID/AI status shown in UI | PASS (values render, not stuck on `—`) |
| History records shown | PASS |
| Static JS not stale across releases | PASS (`?v={app_version}` + `no-cache`) |
| Works after reload / server restart | PASS |
| No OS-path-dependent frontend code | PASS (same-origin relative only) |

**FRONTEND VALIDATION: PASS.**
