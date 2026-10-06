# activate.ps1 - Pixiv 自动下载启动脚本
# 用法: .\activate.ps1 [auto_download.py 的参数]
# 示例:
#   .\activate.ps1                     # 使用默认 AnchorList.csv
#   .\activate.ps1 custom_list.csv     # 使用自定义锚点列表
#   .\activate.ps1 --test              # 仅测试通知功能

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$venvPython = Join-Path $scriptDir "pixiv_venv\Scripts\python.exe"
$venvActivate = Join-Path $scriptDir "pixiv_venv\Scripts\Activate.ps1"

if (-not (Test-Path $venvPython)) {
    Write-Host "[错误] 找不到虚拟环境: $venvPython" -ForegroundColor Red
    Write-Host "请先创建虚拟环境: python -m venv pixiv_venv"
    exit 1
}

if (Test-Path $venvActivate) {
    & $venvActivate
}

Write-Host "[Pixiv] 当前目录: $scriptDir" -ForegroundColor Cyan
Write-Host "[Pixiv] Python: $venvPython" -ForegroundColor Cyan
Write-Host ""

& $venvPython "PixivUtil2.py" @args

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[Pixiv] 脚本以退出码 $LASTEXITCODE 结束" -ForegroundColor Yellow
}

if (Test-Path $venvActivate) {
    deactivate
}