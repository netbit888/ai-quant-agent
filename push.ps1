# ============================================================
#  AI Quant Agent  -  一键提交 + 推送
#
#  由 push.bat 调用（双击 push.bat 即可）。单独运行：
#      powershell -NoProfile -ExecutionPolicy Bypass -File push.ps1
#
#  设计约束（都是踩过的坑）：
#   - 逻辑不能写在 .bat 里：中文会让 cmd.exe 读错自己的行；
#   - .bat 里不能 chcp：会把子进程的 stdin 吞掉，Read-Host 只能
#     读到空字符串，提交说明会静默丢失；
#   - 所以这里**不改控制台代码页**：提示文字按控制台默认编码显示，
#     你输入的中文也按默认编码读进来（Windows 内部是 UTF-16，
#     不会有损失），只在写进文件时转成 UTF-8 交给 git。
#   - git 的回显用 -q 关掉：它的输出是 UTF-8，在 GBK 控制台里会乱码，
#     该显示的摘要我们用自己的字符串打印。
#
#  文件本身必须以 UTF-8 with BOM 保存，否则 Windows PowerShell 5.1
#  会把中文提示按 ANSI 解码成乱码。
# ============================================================
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Stop-With([string]$message, [int]$code = 1) {
    Write-Host ""
    Write-Host "[错误] $message" -ForegroundColor Red
    Write-Host ""
    Read-Host "按回车关闭窗口" | Out-Null
    exit $code
}

function Pause-And-Exit([int]$code = 0) {
    Write-Host ""
    Read-Host "按回车关闭窗口" | Out-Null
    exit $code
}

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  AI Quant Agent  -  提交 + 推送" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# ---- 1. 仓库与分支 ----
$branch = (& git rev-parse --abbrev-ref HEAD 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $branch) { Stop-With "这里不是 git 仓库：$PSScriptRoot" }
$branch = $branch.Trim()
Write-Host "分支：$branch"
Write-Host ""

# ---- 2. 看改了什么（可能包含临时文件，所以下面还要确认一次）----
& git status --short
Write-Host ""

$changes = & git status --porcelain
if (-not $changes) {
    Write-Host "没有需要提交的改动，工作区是干净的。" -ForegroundColor Green
    Pause-And-Exit 0
}

# ---- 3. 确认 ----
$go = Read-Host "确认这些内容？按回车继续，输入 n 取消"
if ($go -and $go.Trim().ToLower() -eq 'n') {
    Write-Host "已取消，什么都没改。"
    Pause-And-Exit 0
}

# ---- 4. 提交说明 ----
$msg = Read-Host "提交说明（例如 fix: 修备注被刷新冲掉）"
if (-not $msg -or -not $msg.Trim()) { Stop-With "提交说明不能为空，什么都没有提交。" }
$msg = $msg.Trim()

# 写成 UTF-8 文件再交给 git -F：
#   - 保证中文按 UTF-8 入库（否则 GitHub 上是乱码）
#   - 说明里有引号 / & / | 也不会破坏命令
$msgFile = Join-Path $env:TEMP 'aqa_commit_msg.txt'
[IO.File]::WriteAllText($msgFile, $msg, (New-Object System.Text.UTF8Encoding($false)))

try {
    # ---- 5. 提交 ----
    & git add -A
    if ($LASTEXITCODE -ne 0) { Stop-With "git add 失败，没有提交。" }

    & git commit -q -F $msgFile
    if ($LASTEXITCODE -ne 0) { Stop-With "提交失败（可能被 hook 拦下），没有推送。" }

    $hash = (& git rev-parse --short HEAD).Trim()
    Write-Host ""
    Write-Host "已提交 $hash" -ForegroundColor Green
    Write-Host "  $msg" -ForegroundColor DarkGray

    # ---- 6. 推送 ----
    Write-Host ""
    Write-Host "推送到 origin/$branch ..." -ForegroundColor Yellow
    & git push origin $branch
    if ($LASTEXITCODE -ne 0) { Stop-With "推送失败。提交已经保存在本地，修好问题后运行：git push" }

    Write-Host ""
    Write-Host "完成：已推送到 origin/$branch" -ForegroundColor Green
} finally {
    Remove-Item -LiteralPath $msgFile -ErrorAction SilentlyContinue
}

Pause-And-Exit 0
