# deploy/windows/healthcheck.ps1
# Thay cho Docker HEALTHCHECK: nếu agent không trả lời /api/v1/health 3 lần liên tiếp thì khởi động lại service.
# (WinSW đã tự khởi động lại khi tiến trình CHẾT; script này lo trường hợp tiến trình còn sống nhưng bị treo.)
#
# Đăng ký chạy mỗi 5 phút bằng Task Scheduler (PowerShell Admin, tại thư mục gốc dự án):
#   $a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PWD\deploy\windows\healthcheck.ps1`""
#   $t = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
#   Register-ScheduledTask -TaskName "NynaAgentHealth" -Action $a -Trigger $t -User "SYSTEM" -RunLevel Highest

$Url = "http://127.0.0.1:8080/api/v1/health"
$Log = Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path "logs\healthcheck.log"

$svc = Get-Service -Name NynaAgent -ErrorAction SilentlyContinue
if (-not $svc -or $svc.Status -ne "Running") { exit 0 }   # đang dừng có chủ ý hoặc WinSW đang khởi động lại

for ($i = 1; $i -le 3; $i++) {
    try {
        Invoke-RestMethod -Uri $Url -TimeoutSec 5 | Out-Null
        exit 0
    } catch {
        Start-Sleep -Seconds 10
    }
}

Add-Content -Path $Log -Value "$(Get-Date -Format s) health check thất bại 3 lần, khởi động lại NynaAgent"
Restart-Service -Name NynaAgent -Force
