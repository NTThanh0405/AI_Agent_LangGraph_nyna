# deploy/windows/install-services.ps1
# Cài 2 Windows Service chạy 24/7 bằng WinSW (thay docker compose):
#   NynaAgent       : python run_windows.py agent       (bot Telegram + LangGraph, 127.0.0.1:8080)
#   NynaDashboards  : python run_windows.py dashboards  (web dashboard thay nginx, 127.0.0.1:8081)
# Cấu hình từng service nằm trong deploy\windows\winsw\<Tên>.xml (sửa ở đó, không sửa ở script này).
# Cloudflare Tunnel (cloudflared) cài riêng thành service bằng lệnh "cloudflared service install <TOKEN>".
#
# Chạy bằng PowerShell "Run as Administrator", tại thư mục gốc dự án:
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install-services.ps1 -WinSW C:\tools\WinSW-x64.exe
# -WinSW: file WinSW-x64.exe (bản 2.12) tải từ trang release GitHub của WinSW.
# Chạy lại script này an toàn: service cũ được gỡ rồi cài lại (dùng khi sửa file XML).

param(
    [Parameter(Mandatory = $true)][string]$WinSW
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$SvcDir = Join-Path $PSScriptRoot "winsw"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Logs = Join-Path $Root "logs"
$Data = Join-Path $Root "data"

if (-not (Test-Path $WinSW)) { throw "Không tìm thấy WinSW tại $WinSW" }
if (-not (Test-Path $Python)) { throw "Chưa có venv: chạy 'py -3.14 -m venv .venv' và 'pip install -r requirements.txt' trước" }
if (-not (Test-Path (Join-Path $Root ".env"))) { throw "Chưa có file .env ở $Root" }
if (-not (Test-Path (Join-Path $Root "static\chart.umd.min.js"))) {
    Write-Warning "Thiếu static\chart.umd.min.js: dashboard sẽ không vẽ được biểu đồ"
}

New-Item -ItemType Directory -Force -Path $Logs, (Join-Path $Data "dashboards"), (Join-Path $Data "mpl") | Out-Null

# Quyền thư mục cho LocalService (SID S-1-5-19): đọc toàn bộ dự án, ghi data\ và logs\
icacls $Root /grant "*S-1-5-19:(OI)(CI)RX" | Out-Null
icacls $Data /grant "*S-1-5-19:(OI)(CI)M" | Out-Null
icacls $Logs /grant "*S-1-5-19:(OI)(CI)M" | Out-Null

foreach ($Name in "NynaDashboards", "NynaAgent") {
    $Exe = Join-Path $SvcDir "$Name.exe"
    if (-not (Test-Path (Join-Path $SvcDir "$Name.xml"))) { throw "Thiếu $SvcDir\$Name.xml" }

    # Gỡ bản cũ (nếu có) trước khi thay file exe
    if (Get-Service -Name $Name -ErrorAction SilentlyContinue) {
        Stop-Service -Name $Name -Force -ErrorAction SilentlyContinue
        & $Exe uninstall
        Start-Sleep -Seconds 2
    }

    # WinSW đọc file XML trùng tên: NynaAgent.exe -> NynaAgent.xml
    Copy-Item -Path $WinSW -Destination $Exe -Force
    & $Exe install
    if ($LASTEXITCODE -ne 0) { throw "Cài service $Name thất bại" }
    Start-Service -Name $Name
}

Start-Sleep -Seconds 15
Get-Service NynaAgent, NynaDashboards | Format-Table Name, Status, StartType
try {
    Invoke-RestMethod -Uri "http://127.0.0.1:8080/api/v1/health" -TimeoutSec 5 | Out-Null
    Write-Host "Agent: health OK" -ForegroundColor Green
} catch {
    Write-Warning "Agent chưa trả lời health check, xem $Logs\NynaAgent.err.log và NynaAgent.out.log"
}
