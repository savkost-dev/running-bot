# deploy.ps1 - polnyy deploy running-bot na DigitalOcean
# VAZNO: vsegda ispolzovat etot skript, nikogda ne scp otdelnyye .py fayly vruchnuyu

$SSH_KEY    = "$env:USERPROFILE/.ssh/digitalocean"
$REMOTE     = "root@167.172.185.88"
$SRC_LOCAL  = "D:/running-bot/src"
$SRC_REMOTE = "/opt/running-bot/src"

# -- 1. Obnovit BUILD_DATE v version.py --
$today = (Get-Date -Format "yyyy-MM-dd")
$versionFile = "D:\running-bot\src\version.py"
$enc = [System.Text.Encoding]::UTF8
$content = [System.IO.File]::ReadAllText($versionFile, $enc)
$content = $content -replace 'BUILD_DATE = "[\d-]+"', "BUILD_DATE = `"$today`""
[System.IO.File]::WriteAllText($versionFile, $content, (New-Object System.Text.UTF8Encoding $false))

$version = ((Get-Content $versionFile) | Where-Object { $_ -match '^VERSION' }) -replace '.*"(.*)".*', '$1'
$firstChange = ((Get-Content $versionFile -Encoding UTF8) | Where-Object { $_ -match '^\s+"' } | Select-Object -First 1) -replace '^\s+"(.+?)",?\s*$', '$1'
Write-Host ""
Write-Host "==> Deploy v$version ($today)" -ForegroundColor Cyan

# -- 2. Kopirovat VSE .py fayly + konfigi --
Write-Host "==> Copying files to server..." -ForegroundColor Yellow

scp -i $SSH_KEY "$SRC_LOCAL/*.py" "${REMOTE}:${SRC_REMOTE}/"
scp -i $SSH_KEY "D:/running-bot/.env"         "${REMOTE}:/opt/running-bot/"
scp -i $SSH_KEY "D:/running-bot/CHANGELOG.md" "${REMOTE}:/opt/running-bot/"
scp -i $SSH_KEY "D:/running-bot/CLAUDE.md"    "${REMOTE}:/opt/running-bot/"

# -- 3. Restart --
Write-Host "==> Restarting running-bot..." -ForegroundColor Yellow
ssh -i $SSH_KEY $REMOTE "systemctl restart running-bot"
Start-Sleep -Seconds 3

# -- 4. Verification --
Write-Host "==> Server verification:" -ForegroundColor Yellow

$health      = ssh -i $SSH_KEY $REMOTE "curl -s http://localhost:8080/health"
$whoop_uri   = ssh -i $SSH_KEY $REMOTE "grep WHOOP_REDIRECT_URI /opt/running-bot/src/whoop.py | head -1"
$strava_base = ssh -i $SSH_KEY $REMOTE "grep OAUTH_REDIRECT_BASE /opt/running-bot/src/strava.py | head -1"
$ai_default  = ssh -i $SSH_KEY $REMOTE "grep ai_mode /opt/running-bot/src/database.py | grep DEFAULT | head -1"
$log_tail    = ssh -i $SSH_KEY $REMOTE "journalctl -u running-bot -n 3 --no-pager 2>&1"

Write-Host "    Health  : $health"
Write-Host "    Whoop   : $whoop_uri"
Write-Host "    Strava  : $strava_base"
Write-Host "    AI mode : $ai_default"
Write-Host ""
Write-Host "    Log:"
Write-Host $log_tail
Write-Host ""

if ($health -like "*$version*") {
    Write-Host "OK Deploy v$version ($today)" -ForegroundColor Green
} else {
    Write-Host "WARN: health=$health expected version=$version" -ForegroundColor Red
}
Write-Host ""

# -- 5. Git commit + push --
Write-Host "==> Committing to GitHub..." -ForegroundColor Yellow
git add -A
$gitStatus = git status --porcelain
if ($gitStatus) {
    git commit -m "deploy: v$version - $firstChange"
    if ($LASTEXITCODE -eq 0) {
        git push origin master
        if ($LASTEXITCODE -eq 0) {
            Write-Host "OK Git: pushed v$version to origin/master" -ForegroundColor Green
        } else {
            Write-Host "WARN: git push failed" -ForegroundColor Red
        }
    } else {
        Write-Host "WARN: git commit failed" -ForegroundColor Red
    }
} else {
    Write-Host "INFO: Nothing to commit" -ForegroundColor Gray
}
Write-Host ""
