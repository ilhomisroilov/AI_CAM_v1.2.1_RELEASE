# Build Provenance

- Release: AI_CAM v1.2.1
- Source commit: `2e814577430b889feaf7de8509b42a17f228b769`
- Source branch: `release/v1.2.1-ocr-engine`
- Consolidated root: `AI_CAM_v1.2.1_RELEASE`
- Independent `.git` directory: verified before consolidation
- Original repository/worktree/dataset directories: read-only and unchanged by
  consolidation
- Dependency source: `requirements-lock.txt`, CPython 3.11, CPU-safe profile
- Packaging: PyInstaller ONEDIR via `AI_CAM_v1.2.1.spec`
- Model checksums: recorded in `reports/CONTINUATION_CHECKPOINT.md` and the final
  release report

The final clean branch is created as an orphan root after tests, packaging,
security review and staged-file inspection.
