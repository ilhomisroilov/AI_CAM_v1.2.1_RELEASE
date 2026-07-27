# PLC integration and configuration — AI_CAM v1.1.3

AI_CAM uses the PLC only to control production sessions. YOLO and OCR models remain loaded;
PLC transitions do not reload models. Environment values live in `config/settings.yaml`, with
typed defaults and validation in `backend/config.py`.

## Current line flow

```text
Mitsubishi D2222 word, configured bit
        │  PLCService polls every poll_interval_ms
        ▼
0 → 1 rising edge
        ├── open one pipeline session
        ├── start RFID immediately
        └── start camera after camera_delay_sec
                │
                ├── VIN/RFID result
                └── configured hard deadline
                        ▼
                 finalize one DB record
```

The current production profile uses `signal_address: D2222`, `signal_kind: bit`,
`signal_bit: 0` and `trigger_mode: pulse`. In pulse mode the falling edge ends the pulse, not
the session; the session continues until its result/deadline policy closes it. In level mode a
falling edge may stop processing.

For compatibility with earlier hardening/HIL tools, v1.1.3 also accepts the legacy
`trigger_kind`/`trigger_bit`, `read_register`/`write_bit`, `PLCHandshakeState` and
`PLCConfigError` contracts. `trigger_kind` is `None` in the current profile, so these adapters do
not override `signal_kind` or D2222 pulse behavior. RFID uses a session-owned `RFIDResult` while
temporarily supporting the earlier three-argument callback during integration.

## Signal interpretation

Never compare a Mitsubishi word register blindly with `1` when unrelated bits may be set.
`signal_kind` controls how the raw value is interpreted:

| Mode | Meaning |
|---|---|
| `value` | Entire register equals `trigger_on_value`. |
| `bit` | Only `signal_bit` is evaluated. Recommended for D2222 in the current profile. |
| `mask` | `(value & trigger_mask)` is compared with the masked trigger value. |
| `auto` | Selects mask, then bit, then value according to configured fields. |

The PLC/controls owner must confirm the real address, bit index, pulse width and latch behavior.
Polling cannot recover a physical pulse that begins and ends entirely between two polls; the PLC
ladder must latch such a pulse or hold it long enough for acknowledgement.

## Rearm and DONE handshake

`require_zero_before_rearm: true` prevents a held/stale signal from opening repeated sessions.
After session finalization the service can write `done_value` to `done_address`. An empty
`done_address` disables that physical write; it must not be filled with a guessed register.

Before enabling PLC writes in production, verify all of the following in HIL:

1. D2222 address and bit are correct while other word bits toggle.
2. One physical body produces exactly one rising-edge event and one DB session.
3. A stuck-high signal never creates a trigger storm.
4. Disconnect/reconnect does not replay a stale body event.
5. DONE address, value and ladder clear behavior are signed off by the controls owner.
6. A pulse that occurs during active processing follows the agreed queue/reject policy.

## Simulator and API

Use `plc.mode: simulator` for software-only checks. The dashboard calls:

- `POST /api/plc/sim` with `{ "state": 0|1 }`;
- `GET /api/plc/status` for connection and signal state.

Do not use simulator success as evidence that Mitsubishi word/bit addressing or line timing is
correct. Those are HIL gates.

## Configuration ownership

| Section | Main responsibility |
|---|---|
| `camera` | SICK address, CoLa/BLOB ports and credentials. |
| `plc` | Mitsubishi transport, trigger interpretation, timing, rearm and DONE. |
| `rfid` | R700 connection and inventory windows. |
| `detection`, `ocr`, `vin` | Vision thresholds and validation policy. |
| `session` | Session deadline, result requirements and retention behavior. |
| `server`, `auth` | HTTP binding and access control. |

Settings saved through the UI may require an application restart for model, camera, PLC or server
objects to be rebuilt. Production secrets must be supplied through the approved deployment
process and must not be pasted into logs or documentation.

## Verification commands

```powershell
# Software-only PLC contracts
python -m pytest tests/test_plc_handshake.py tests/test_plc_chaos.py `
  tests/test_plc_exit_signal.py tests/test_d2222_hard_deadline.py -q

# Full runtime readiness (see the deployment guide)
python tools/runtime_self_check.py
```

The project currently contains test/evidence artifacts from multiple hardening baselines. A green
historical XML file is not proof that the current worktree passes. Run the tests again after all
v1.1.3 integration changes are merged, then execute the hardware checks in
`docs/HIL_PRODUCTION_VALIDATION_PLAN.md`.

`tests/test_plc_pulse_latching.py` is an older level-cycle contract: two tests require `on_stop`
on pulse falling-edge and another assumes `PLCSimulator.set_state(1→0)` is not latched. Both
expectations contradict the current D2222 pulse profile, where falling-edge does not close an
active session and the simulator intentionally preserves a short rising pulse. The tests are kept
as historical evidence and must be rebaselined by the test owner; production semantics were not
regressed merely to turn them green.
