# ---------------------------------------------------------------------------
# 一键启动 SixCoder：后端(Python) + 桌面壳(Electron)
#
#   .\start.ps1               默认：起后端 + 开桌面窗口
#   .\start.ps1 -Web          只起后端 + 用浏览器打开（不装 Electron）
#   .\start.ps1 -NoShell      只起后端（调试/CI）
#   .\start.ps1 -Port 8770    换端口
#   .\start.ps1 -Rebuild      强制重建前端 bundle
#   .\stop.ps1                停掉后端与窗口
#
# 兼容 Windows PowerShell 5.1（不用 ?? 与三元运算符）。
# ---------------------------------------------------------------------------
[CmdletBinding()]
param(
    [int]$Port = 8760,
    [switch]$Web,
    [switch]$NoShell,
    [switch]$Rebuild
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$Backend = Join-Path $Root 'prototype'
$Shell = Join-Path $Root 'shell'
$WebDir = Join-Path $Backend 'web'
$LogDir = Join-Path $Root '_logs'
$PidFile = Join-Path $LogDir 'backend.pid'
$HealthUrl = "http://127.0.0.1:$Port/v1/health"

function Write-Step($msg) { Write-Host "  $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "  [OK]   $msg" -ForegroundColor Green }
function Write-Warn2($msg) { Write-Host "  [警告] $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "  [失败] $msg" -ForegroundColor Red }

function Resolve-Python {
    if ($env:SIXCODER_PYTHON -and (Test-Path $env:SIXCODER_PYTHON)) { return $env:SIXCODER_PYTHON }
    $candidates = @('D:\Python\python.exe', 'C:\Python312\python.exe', 'C:\Python311\python.exe')
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Get-BackendHealth {
    try {
        $r = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { return $r.Content }
    } catch { }
    return $null
}

Write-Host ""
Write-Host "SixCoder 启动" -ForegroundColor White
Write-Host "  仓库根：$Root"
Write-Host ""

# ---------------------------------------------------------------- 1. Python
Write-Step '检查 Python…'
$python = Resolve-Python
if (-not $python) {
    Write-Fail ' 找不到 Python。请安装 Python 3.10+，或设置环境变量 SIXCODER_PYTHON 指向 python.exe'
    exit 1
}
Write-Ok "$python"

# ---------------------------------------------------------------- 2. 前端产物
if (-not $NoShell) {
    Write-Step '检查前端 bundle…'
    $distIndex = Join-Path $WebDir 'dist\index.html'
    $needBuild = $Rebuild -or (-not (Test-Path $distIndex))
    if ($needBuild) {
        if (-not (Test-Path (Join-Path $WebDir 'node_modules'))) {
            Write-Step ' 首次运行：安装前端依赖（npm install，约 1 分钟）…'
            Push-Location $WebDir
            try { & npm install --no-audit --no-fund 2>&1 | Out-Null } finally { Pop-Location }
        }
        Write-Step ' 构建前端 bundle（npm run build）…'
        Push-Location $WebDir
        try { & npm run build 2>&1 | Out-Null } finally { Pop-Location }
        if (-not (Test-Path $distIndex)) { Write-Fail ' 前端构建失败：dist\index.html 不存在'; exit 1 }
        Write-Ok ' 前端已构建'
    } else {
        Write-Ok ' 已有 dist（加 -Rebuild 可强制重建）'
    }
}

# ---------------------------------------------------------------- 3. 后端
Write-Step "检查后端（端口 $Port）…"
$health = Get-BackendHealth
$startedBackend = $false
if ($health) {
    Write-Ok ' 后端已在运行，直接复用'
} else {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $outLog = Join-Path $LogDir "backend-$Port.out.log"
    $errLog = Join-Path $LogDir "backend-$Port.err.log"
    $args = @('server.py', '--port', "$Port")
    $proc = Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $Backend `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    Set-Content -Path $PidFile -Value $proc.Id -Encoding ASCII
    $startedBackend = $true
    Write-Step " 已拉起后端（PID $($proc.Id)），等待就绪…"
    $ok = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        if (Get-BackendHealth) { $ok = $true; break }
        if ($proc.HasExited) { break }
    }
    if (-not $ok) {
        Write-Fail " 后端没能起来（端口 $Port）。日志："
        if (Test-Path $errLog) { Get-Content $errLog -Tail 20 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray } }
        if (Test-Path $outLog) { Get-Content $outLog -Tail 20 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray } }
        exit 1
    }
    Write-Ok ' 后端就绪'
}

# 端口的真实健康信息（Ollama/模型/门槛）
try {
    $h = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 3
    $ollama = '离线'
    if ($h.ollama) { $ollama = '在线' }
    Write-Host "         Ollama: $ollama｜默认工人: $($h.default_model)"
    if ($h.model_policy -and $h.model_policy.blocked) {
        $blocked = $h.model_policy.blocked -join ', '
        if ($blocked) { Write-Warn2 "低于门槛不可选：$blocked" }
    }
    if (-not $h.ollama) {
        Write-Warn2 'Ollama 不可达：界面能开，但生成/审查会失败。另开一个窗口运行：ollama serve'
    }
} catch { }

# ---------------------------------------------------------------- 4. 外壳
$url = "http://127.0.0.1:$Port/"
if ($NoShell) {
    Write-Host ""
    Write-Ok "后端已就绪：$url（-NoShell：不打开界面）"
    exit 0
}

if ($Web) {
    Write-Step '用浏览器打开…'
    Start-Process $url
    Write-Ok "已打开 $url"
    exit 0
}

Write-Step '启动桌面窗口（Electron）…'
if (-not (Test-Path (Join-Path $Shell 'node_modules\electron'))) {
    Write-Step ' 首次运行：安装 Electron（npm install，可能需要几分钟）…'
    Push-Location $Shell
    try { & npm install --no-audit --no-fund 2>&1 | Out-Null } finally { Pop-Location }
    if (-not (Test-Path (Join-Path $Shell 'node_modules\electron'))) {
        Write-Warn2 ' Electron 安装失败，退化为浏览器方式'
        Start-Process $url
        exit 0
    }
}
$env:SIXCODER_PORT = "$Port"
$env:SIXCODER_BACKEND = $Backend
$env:SIXCODER_PYTHON = $python
Push-Location $Shell
try {
    & npm start
} finally {
    Pop-Location
}

# 窗口关闭后：如果后端是本脚本拉起的，一并收掉
if ($startedBackend) {
    Write-Step '窗口已关闭，停止本脚本拉起的后端…'
    & (Join-Path $Root 'stop.ps1') -Quiet
}
