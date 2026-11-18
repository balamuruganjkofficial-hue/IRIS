param([Parameter(Mandatory=$true)][string]$Message)
Set-Location C:\dev\IRIS
Write-Host "1/4 compile and tests"
venv\Scripts\python.exe -m compileall -q main.py core skills interfaces tests
if ($LASTEXITCODE -ne 0) { Write-Host "Compile failed - not committing"; exit 1 }
venv\Scripts\python.exe -m unittest discover -s tests
if ($LASTEXITCODE -ne 0) { Write-Host "Tests failed - not committing"; exit 1 }
git add -A
Write-Host "2/4 secret check"
$bad = git diff --cached --name-only | Select-String -Pattern '(^|/)\.env$|\.pem$|keys/|browser_profile'
$leak = git diff --cached | Select-String -Pattern 'gsk_[A-Za-z0-9]{20,}|sk-or-[A-Za-z0-9-]{20,}|AIza[0-9A-Za-z_-]{30,}'
if ($bad -or $leak) { Write-Host "Possible secret or private file staged - unstaging everything"; git reset -q; $bad; exit 1 }
Write-Host "3/4 what changed"
git diff --cached --stat
git commit -m $Message
Write-Host "4/4 done"
