# Обновляем BUILD_DATE в version.py (UTF-8 без BOM)
$today = (Get-Date -Format "yyyy-MM-dd")
$versionFile = "D:\running-bot\src\version.py"
$enc = [System.Text.Encoding]::UTF8
$content = [System.IO.File]::ReadAllText($versionFile, $enc)
$content = $content -replace 'BUILD_DATE = "[\d-]+"', "BUILD_DATE = `"$today`""
[System.IO.File]::WriteAllText($versionFile, $content, (New-Object System.Text.UTF8Encoding $false))

scp -i "C:\Users\savko\.ssh\digitalocean" D:\running-bot\src\*.py root@167.172.185.88:/opt/running-bot/src/
scp -i "C:\Users\savko\.ssh\digitalocean" D:\running-bot\.env root@167.172.185.88:/opt/running-bot/
ssh -i "C:\Users\savko\.ssh\digitalocean" root@167.172.185.88 "systemctl restart running-bot"

$version = ((Get-Content $versionFile) | Where-Object { $_ -match '^VERSION' }) -replace '.*"(.*)".*', '$1'
Write-Host "Задеплоено! v$version ($today)"
