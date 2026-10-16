# 创建/复用仓库内的隔离虚拟环境并安装签到依赖（不会写入系统默认 Python）。
# 用法:  pwsh -File setup_env.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvDir = ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "创建虚拟环境 $venvDir ..."
    python -m venv $venvDir
}

& $venvPython -m pip install --disable-pip-version-check --no-input --upgrade pip
& $venvPython -m pip install --disable-pip-version-check --no-input -r requirements.txt -r requirements-browser.txt

# Playwright 自带的 Chromium 下载目录放在仓库内，避免污染全局缓存。
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot ".ms-playwright"
$browserReady = $false
try {
    & $venvPython -m playwright install chromium
    $browserReady = $?
} catch {
    Write-Warning "Chromium 下载失败：$($_.Exception.Message)"
}
if (-not $browserReady) {
    Write-Warning "无法下载 Playwright Chromium（常见原因是当前会话限制了子进程/管道）。"
    Write-Warning "本机调试可以改用系统已安装的 Chrome/Edge，不需要下载："
    Write-Warning "  & `"$venvPython`" checkin.py --dry-run --channel chrome"
}

Write-Host ""
Write-Host "完成。使用方式："
Write-Host "  运行单测: & `"$venvPython`" -B -m unittest -q"
Write-Host "  本机调试: `$env:GLADOS_COOKIE='gld:sess=...; gld:sess.sig=...'; & `"$venvPython`" checkin.py --dry-run --channel chrome"
Write-Host "  真正签到: & `"$venvPython`" checkin.py --no-jitter --channel chrome"
