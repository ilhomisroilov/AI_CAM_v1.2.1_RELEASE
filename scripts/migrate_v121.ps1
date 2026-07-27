# Apply the additive v1.2.1 OCR DB migration (idempotent). Safe to re-run.
. "$PSScriptRoot\_v121_common.ps1"
Write-Host "Applying v1.2.1 OCR migration to $Db ..."
& $Py -c "import sqlite3,sys,json; sys.path.insert(0,r'$Repo'); from backend.database import ocr_v121_db as o; c=sqlite3.connect(r'$Db'); print(json.dumps(o.migrate(c))); c.close()"
exit $LASTEXITCODE
