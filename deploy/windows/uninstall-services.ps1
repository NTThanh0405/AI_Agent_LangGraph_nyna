# deploy/windows/uninstall-services.ps1
# Gỡ 2 service NynaAgent và NynaDashboards (không đụng tới cloudflared, .env, data\, logs\).
#   powershell -ExecutionPolicy Bypass -File deploy\windows\uninstall-services.ps1

$SvcDir = Join-Path $PSScriptRoot "winsw"

foreach ($Name in "NynaAgent", "NynaDashboards") {
    $Exe = Join-Path $SvcDir "$Name.exe"
    if ((Get-Service -Name $Name -ErrorAction SilentlyContinue) -and (Test-Path $Exe)) {
        Stop-Service -Name $Name -Force -ErrorAction SilentlyContinue
        & $Exe uninstall
    }
}
