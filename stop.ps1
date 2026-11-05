# ---------------------------------------------------------------------------
# 停止 SixCoder：后端 + 桌面窗口
#   .\stop.ps1           停后端（按 _logs\backend.pid）并关掉 Electron 窗口
#   .\stop.ps1 -Quiet    静默（给 start.ps1 收尾用）
# ---------------------------------------------------------------------------
[CmdletBinding()]
param([switch]$Quiet)

$ErrorActionPreference = 'Continue'
$Root = $PSScriptRoot
$PidFile = Join-Path $Root '_logs\backend.pid'

function Say($msg, $color) { if (-not $Quiet) { Write-Host $msg -ForegroundColor $color } }

$stopped = 0

# 1) 按 pid 文件停后端
if (Test-Path $PidFile) {
    $pidText = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($pidText -match '^\d+$') {
        $p = Get-Process -Id ([int]$pidText) -ErrorAction SilentlyContinue
        if ($p) {
            Say "  停止后端 PID $pidText" Yellow
            Stop-Process -Id ([int]$pidText) -Force -ErrorAction SilentlyContinue
            $stopped++
        }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

# 2) 兜底：按命令行里的 server.py --port 找残留（pid 文件丢了也能停）
$left = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'server\.py' -and $_.CommandLine -match 'SixCoder' }
foreach ($proc in $left) {
    Say "  停止残留后端 PID $($proc.ProcessId)" Yellow
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    $stopped++
}

# 3) 关掉 Electron 外壳（只关本项目目录下的）
$electron = Get-CimInstance Win32_Process -Filter "Name = 'electron.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match 'SixCoder\\shell' }
foreach ($e in $electron) {
    Say "  关闭窗口 PID $($e.ProcessId)" Yellow
    Stop-Process -Id $e.ProcessId -Force -ErrorAction SilentlyContinue
    $stopped++
}

if ($stopped -eq 0) {
    Say '  没有在运行的后端/窗口' DarkGray
} else {
    Say "  已停止 $stopped 个进程" Green
}
