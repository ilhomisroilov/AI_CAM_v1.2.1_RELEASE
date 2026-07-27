# AI_CAM v1.2.1 Release Continuation Checkpoint

Recorded: 2026-07-27 (Asia/Tashkent)

## Repository identity

- Final working root: `C:\Users\II4028\Documents\Projects\AI_CAM_v1.2.1_RELEASE`
- Current branch: `release/v1.2.1-ocr-engine`
- Current commit: `2e814577430b889feaf7de8509b42a17f228b769`
- Source provenance: `2e81457` (`feat(ocr): v1.2.1 shadow OCR integration — 3-engine orchestrator, DB, collector, wiring`)
- Independent Git repository: yes; `.git` is a real directory.
- Worktree state: the only registered worktree is the final release root.
- Remotes: none.

## Interrupted working-tree state

`git diff --shortstat` reported 575 tracked files changed: 40 insertions and
7,168 deletions.

- 571 tracked files are deleted as part of the intended release cleanup
  (training data, training runs, development artifacts and backups).
- Modified tracked files:
  - `backend/ai/ocr_process.py`
  - `backend/ai/ocr_worker.py`
  - `backend/config.py`
  - `config/settings.yaml`
- New untracked release content:
  - `models/paddle/{det,rec,cls}/`
  - `models/yolo/best.pt`
  - runtime `.gitkeep` files under `runtime/`
- `run.py` exists and is still the pre-consolidation launcher. It has a basic
  runtime diagnostic `--self-check`, but no release preflight, `--dry-run`,
  database/migration startup gate, model-contract audit or deterministic
  startup-failure classification.
- `.gitattributes` is missing.

## Completed work confirmed locally

- Runtime/model paths in `backend/config.py` are rooted from the file location,
  not the shell current directory.
- Runtime DB, logs and crop paths point under `runtime/`.
- YOLO points to `models/yolo/best.pt`.
- Engraved model artifacts already exist in `models/engraved_ocr_v1.2.1/`.
- Paddle det/rec/cls artifacts already exist under `models/paddle/`.
- `ocr_worker.py` threads the three Paddle paths toward the process engine.
- `settings.yaml` collection output points to
  `runtime/engraved_ocr_collection`.
- Required runtime directories and `.gitkeep` placeholders exist.
- No fresh runtime database exists yet.

## Model inventory (SHA-256)

- YOLO `models/yolo/best.pt` (6,237,610 bytes):
  `82498EAFE8EA1C631FAAA32EBF8A8AE7767040103E8BDE90BE6241A31FAAEC30`
- Engraved ONNX `models/engraved_ocr_v1.2.1/model.onnx` (6,176,003 bytes):
  `715BAAC1CAC8E4F7E824A6BFC8F2005359D7EBB12596FFD81AA50A0B2D6C3D19`
- Engraved PT `models/engraved_ocr_v1.2.1/best_model.pt` (6,292,530 bytes):
  `9D0BD03743BBB839F0CAD7A37F73F2C738B5CC56FB28D3939E5E458D68B2DF8F`
- Paddle det parameters (2,377,917 bytes):
  `83676EC730627AB4502F401410A4B6A3CE1C0BB98FA249B71DB055B6BDDAE051`
- Paddle det model (1,590,133 bytes):
  `C4BFB1B05D9D1D5A760801EAF6D20180EF7E47BCC675FB17D1F3A89DA5FEF427`
- Paddle rec parameters (7,607,269 bytes):
  `75F64A1FFB70C56B7A25655963CA16F5BF3286202E3F52AC972BEE05CDEE2F56`
- Paddle rec model (2,517,366 bytes):
  `85B952F05F709AF259CFE4254012AA7208BEF0998F71F57A15495446F25CCD43`
- Paddle cls parameters (539,978 bytes):
  `D1EFDA1B80E174B4FCB168A035AC96C1AF4938892BD86A55F300A6027105D08C`
- Paddle cls model (1,624,487 bytes):
  `3C4337EC61722A20B1DCA2E5BFAFFC313C0592BC89AD6E0D45168224186F6683`

## Resolved paths

- Project root: `C:\Users\II4028\Documents\Projects\AI_CAM_v1.2.1_RELEASE`
- Runtime root: `C:\Users\II4028\Documents\Projects\AI_CAM_v1.2.1_RELEASE\runtime`
- Database: `runtime\data\ai_cam.db`
- Logs: `runtime\logs`
- Crops: `runtime\crops`
- YOLO: `models\yolo\best.pt`
- Engraved ONNX: `models\engraved_ocr_v1.2.1\model.onnx`
- Paddle det: `models\paddle\det`
- Paddle rec: `models\paddle\rec`
- Paddle cls: `models\paddle\cls`

All resolved production model/runtime paths are currently below the final release
root.

## Unfinished tasks

- Finish the canonical `run.py` release launcher and startup reports.
- Remove Paddle cache fallback paths and prove explicit local model loading.
- Finish frontend polling ownership fixes and deterministic tests.
- Finish OCR retry evidence deduplication, plate-exit terminal behavior, real
  RAW/ENHANCED invocation and common-input orchestration tests.
- Verify clean DB initialization and idempotent v1.2.1 migration at startup.
- Create the release-local virtual environment and lock/install scripts.
- Run source self-check, dry-run, controlled startup and full test suite.
- Build and verify the ONEDIR executable.
- Finish release documentation, security/runtime scans and Git LFS setup.
- Create the clean orphan root commit, expire old history and verify repository
  size/content.
- Push only if a valid GitHub remote and authentication are available.

## Current blockers and release risks

- The current shell Python lacks `PyYAML`; a release-local environment has not
  been created yet.
- Current Paddle constructor fallbacks can omit explicit model directories and
  therefore violate the no-user-cache requirement.
- `config/settings.yaml` contains deployment credentials/password defaults that
  must not ship as secrets.
- No GitHub remote is configured.
- Git LFS availability has not yet been verified.
- No source or executable acceptance command has yet been executed successfully
  in this continuation.
