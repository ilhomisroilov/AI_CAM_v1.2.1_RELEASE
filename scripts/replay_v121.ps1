# Run the offline v1.2.1 shadow-OCR replay (no PLC/camera/RFID needed). Exits non-zero on failure.
. "$PSScriptRoot\_v121_common.ps1"
& $Py (Join-Path $Repo "tools\replay_v121.py")
exit $LASTEXITCODE
