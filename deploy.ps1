# deploy.ps1 - polnyy deploy running-bot na DigitalOcean
# VAZNO: vsegda ispolzovat etot skript, nikogda ne scp otdelnyye .py fayly vruchnuyu
#
# 06.10.2026: skript OSTANAVLIVAETSYA pri obryve svyazi (scp/ssh vernul oshibku) -
# bez restarta i bez git commit. Posle kopirovaniya sveryaet md5 vsekh src/*.py
# s serverom. Commit/push tolko esli /health otdal novuyu versiyu.
# Rezhimy II proveryayutsya po baze (bot_settings.preprocess_mode, user_preferences.ai_mode),
# a ne grep-om DEFAULT po kodu.

$SSH_KEY    = "$env:USERPROFILE/.ssh/digitalocean"
$REMOTE     = "root@167.172.185.88"
$SRC_LOCAL  = "D:/running-bot/src"
$SRC_REMOTE = "/opt/running-bot/src"

function Stop-IfFailed($step) {
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "STOP: $step failed (exit code $LASTEXITCODE)." -ForegroundColor Red
        Write-Host "      Server NOT restarted, nothing committed. Check connection and run .\deploy.ps1 again." -ForegroundColor Red
        Write-Host ""
        exit 1
    }
}

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

# -- 2. Kopirovat VSE .py fayly + konfigi (lyubaya oshibka scp = stop) --
Write-Host "==> Copying files to server..." -ForegroundColor Yellow

scp -i $SSH_KEY "$SRC_LOCAL/*.py" "${REMOTE}:${SRC_REMOTE}/"
Stop-IfFailed "scp src/*.py"
scp -i $SSH_KEY "D:/running-bot/.env"         "${REMOTE}:/opt/running-bot/"
Stop-IfFailed "scp .env"
scp -i $SSH_KEY "D:/running-bot/CHANGELOG.md" "${REMOTE}:/opt/running-bot/"
Stop-IfFailed "scp CHANGELOG.md"
scp -i $SSH_KEY "D:/running-bot/CLAUDE.md"    "${REMOTE}:/opt/running-bot/"
Stop-IfFailed "scp CLAUDE.md"
# 09.10.2026: vybor prazdnika, zapisannyj botom na servere (stroki s done:), vazhnee lokalnogo kalendarya -
# zabiraem ego v lokalnyj fajl pered kopirovaniem, inache scp -r ego zatrjot
$holLocal = "D:/running-bot/assets/holidays.txt"
$holTmp   = Join-Path $env:TEMP "holidays_server.txt"
if (Test-Path $holTmp) { Remove-Item $holTmp -Force }
scp -i $SSH_KEY "${REMOTE}:/opt/running-bot/assets/holidays.txt" $holTmp
if (Test-Path $holTmp) {
    $srv = @{}
    foreach ($l in [System.IO.File]::ReadAllLines($holTmp, $enc)) {
        if ($l -match "^(\d\d-\d\d)\t.*\tdone:\d{4}\s*$") { $srv[$matches[1]] = $l }
    }
    if ($srv.Count -gt 0) {
        $out = New-Object System.Collections.Generic.List[string]
        foreach ($l in [System.IO.File]::ReadAllLines($holLocal, $enc)) {
            if ($l -match "^(\d\d-\d\d)\t" -and $srv.ContainsKey($matches[1])) { $out.Add($srv[$matches[1]]); $srv.Remove($matches[1]) }
            else { $out.Add($l) }
        }
        foreach ($k in @($srv.Keys)) { $out.Add($srv[$k]) }
        [System.IO.File]::WriteAllText($holLocal, (($out -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding $false))
        Write-Host "    holidays.txt: vybor admina s servera perenesjon v lokalnyj fajl" -ForegroundColor Yellow
    }
    Remove-Item $holTmp -Force
}
# 06.10.2026: fony kartochki i kalendar prazdnikov (assets/) - *.py ikh ne lovit
scp -r -i $SSH_KEY "D:/running-bot/assets"    "${REMOTE}:/opt/running-bot/"
Stop-IfFailed "scp assets/"

# -- 2b. Sverka md5 src/*.py lokalno i na servere (lovit chastichnoe kopirovanie) --
# Kontsy strok (CR) ne schitayutsya: lokalno git autocrlf daet CRLF, na servere LF, dlya Python eto odno i to zhe.
# Python-skript uhodit cherez stdin, chtoby ne eskeypit kavychki PowerShell -> bash.
Write-Host "==> Verifying copied files (md5)..." -ForegroundColor Yellow
$pyMd5 = @'
import hashlib, glob, os
os.chdir('/opt/running-bot/src')
for f in sorted(glob.glob('*.py')):
    print(hashlib.md5(open(f, 'rb').read().replace(b'\r', b'')).hexdigest(), f)
'@
$remoteLines = $pyMd5 | ssh -i $SSH_KEY $REMOTE "python3 -"
Stop-IfFailed "ssh md5 check"
$remoteMd5 = @{}
foreach ($line in $remoteLines) {
    if ($line -match '^([0-9a-f]{32})\s+(.+)$') { $remoteMd5[$matches[2].Trim()] = $matches[1] }
}
$md5 = [System.Security.Cryptography.MD5]::Create()
function Get-Md5NoCR($path) {
    $bytes = [System.IO.File]::ReadAllBytes($path)
    $ms = New-Object System.IO.MemoryStream
    foreach ($b in $bytes) { if ($b -ne 13) { $ms.WriteByte($b) } }
    return ([System.BitConverter]::ToString($md5.ComputeHash($ms.ToArray())) -replace '-', '').ToLower()
}
$localFiles = Get-ChildItem "$SRC_LOCAL/*.py"
$mismatch = @()
foreach ($f in $localFiles) {
    if ($remoteMd5[$f.Name] -ne (Get-Md5NoCR $f.FullName)) { $mismatch += $f.Name }
}
if ($mismatch.Count -gt 0) {
    Write-Host ""
    Write-Host "STOP: files on server differ from local: $($mismatch -join ', ')" -ForegroundColor Red
    Write-Host "      Server NOT restarted, nothing committed. Run .\deploy.ps1 again." -ForegroundColor Red
    Write-Host ""
    exit 1
}
Write-Host "    md5 OK: $($localFiles.Count) files match"

# -- 3. Restart --
Write-Host "==> Restarting running-bot..." -ForegroundColor Yellow
ssh -i $SSH_KEY $REMOTE "systemctl restart running-bot"
Stop-IfFailed "ssh systemctl restart"
Start-Sleep -Seconds 3

# -- 4. Verification --
Write-Host "==> Server verification:" -ForegroundColor Yellow

$health      = ssh -i $SSH_KEY $REMOTE "curl -s http://localhost:8080/health"
$whoop_uri   = ssh -i $SSH_KEY $REMOTE "grep WHOOP_REDIRECT_URI $SRC_REMOTE/whoop.py | head -1"
$strava_base = ssh -i $SSH_KEY $REMOTE "grep OAUTH_REDIRECT_BASE $SRC_REMOTE/strava.py | head -1"
$log_tail    = ssh -i $SSH_KEY $REMOTE "journalctl -u running-bot -n 3 --no-pager 2>&1"

# Rezhimy II - iz bazy na servere, a ne iz koda. Python-skript uhodit cherez stdin,
# chtoby ne eskeypit kavychki PowerShell -> bash -> python.
$pyModes = @'
import sqlite3
c = sqlite3.connect('/opt/running-bot/running_bot.db')
row = c.execute("select value from bot_settings where key='preprocess_mode'").fetchone()
print('Step1 preprocess_mode =', row[0] if row else '(not set)')
rows = c.execute("select coalesce(ai_mode,'(null)'), count(*) from user_preferences where coalesce(is_active,1)=1 group by 1 order by 2 desc").fetchall()
print('Step2 ai_mode (active users) =', ', '.join(m + '=' + str(n) for m, n in rows))
'@
$db_modes = $pyModes | ssh -i $SSH_KEY $REMOTE "python3 -"

Write-Host "    Health  : $health"
Write-Host "    Whoop   : $whoop_uri"
Write-Host "    Strava  : $strava_base"
foreach ($m in $db_modes) { Write-Host "    DB mode : $m" }
Write-Host ""
Write-Host "    Log:"
Write-Host $log_tail
Write-Host ""

if ($health -like "*$version*") {
    Write-Host "OK Deploy v$version ($today)" -ForegroundColor Green
} else {
    Write-Host "STOP: health=$health expected version=$version" -ForegroundColor Red
    Write-Host "      Bot is running old or no code. Nothing committed. Check 'journalctl -u running-bot -n 50' and run .\deploy.ps1 again." -ForegroundColor Red
    Write-Host ""
    exit 1
}
Write-Host ""

# -- 5. Git commit + push (tolko posle uspeshnoy verifikatsii) --
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
