<#
.SYNOPSIS
  安装包 / 可执行文件代码签名脚本（可选步骤）

.DESCRIPTION
  本脚本对最终产物（Inno Setup 安装包 installer\GoofishMasterDesktop-Setup-1.0.0.exe
  以及主程序 GoofishMasterDesktop.exe）进行 Authenticode 签名。

  设计原则：签名是「可选增强」，默认跳过 —— 没有真实 CA 证书时，自签名证书
  仍会显示“未知发布者”，对最终用户零信任收益，反而制造误导，因此本脚本在缺少
  有效证书时仅输出指引并退出（exit 0），不会产出自签名签名。

  要真正启用签名，需满足：
    1. 已安装 Windows SDK（含 signtool.exe）；或显式指定 SignToolPath
    2. 持有由受信任 CA 签发的代码签名证书（EV 证书体验最佳，普通 OV 也可）
       —— 推荐渠道：DigiCert / Sectigo / GlobalSign 等；个人可用免费
          Certum / 或 GitHub 组织的证书。自签名证书不适用于发行场景。

.PARAMETER InstallerPath
  安装包路径（默认 installer\GoofishMasterDesktop-Setup-1.0.0.exe）

.PARAMETER SignToolPath
  显式指定 signtool.exe 路径；默认从 Windows SDK 常见路径探测

.PARAMETER Thumbprint
  用于签名的证书指纹（位于 Cert:\CurrentUser\My 或 LocalMachine\My）。
  不传则不签名，仅打印指引。

.PARAMETER TimestampServer
  RFC3161 时间戳服务，保证证书过期后签名仍有效。默认 DigiCert。

.EXAMPLE
  .\sign.ps1 -Thumbprint "ABCD1234..."            # 用指定证书签名
  .\sign.ps1 -Skip                                # 显式跳过（等同默认）
#>
param(
  [string]$InstallerPath = "installer\GoofishMasterDesktop-Setup-1.0.0.exe",
  [string]$SignToolPath  = "",
  [string]$Thumbprint    = "",
  [string]$TimestampServer = "http://timestamp.digicert.com",
  [switch]$Skip
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition

function Find-SignTool {
  if ($SignToolPath -and (Test-Path $SignToolPath)) { return $SignToolPath }
  $cands = @(
    "signtool.exe",
    "C:\Program Files (x86)\Windows Kits\10\bin\x64\signtool.exe",
    "C:\Program Files (x86)\Windows Kits\10\bin\10.0.22621.0\x64\signtool.exe",
    "C:\Program Files (x86)\Microsoft SDKs\Windows\v10.0A\bin\NETFX 4.8 Tools\signtool.exe"
  )
  foreach ($c in $cands) {
    if ($c -eq "signtool.exe") { $found = Get-Command signtool -ErrorAction SilentlyContinue; if ($found) { return $found.Source } }
    elseif (Test-Path $c) { return $c }
  }
  return ""
}

if ($Skip -or -not $Thumbprint) {
  Write-Host "======================================================"
  Write-Host " 代码签名：跳过（未配置证书）"
  Write-Host "======================================================"
  Write-Host "未提供证书指纹，本次不签名。这是安全的默认行为："
  Write-Host "  - 自签名证书仍会显示“未知发布者”，对发行无信任收益；"
  Write-Host "  - 干净的安装包 + Microsoft 签名的 WebView2 离线安装器"
  Write-Host "    已足够可靠，UAC 不再触发 0x80070005。"
  Write-Host ""
  Write-Host "如需启用真实签名（推荐购买 DigiCert/Sectigo/GlobalSign 等 CA 证书）："
  Write-Host "  1. 安装 Windows SDK（含 signtool.exe）"
  Write-Host "  2. 将证书导入 证书存储(CurrentUser\My 或 LocalMachine\My)"
  Write-Host "  3. 运行： .\sign.ps1 -Thumbprint <证书指纹>"
  Write-Host "======================================================"
  exit 0
}

$st = Find-SignTool
if (-not $st) {
  Write-Warning "未找到 signtool.exe，无法签名。请先安装 Windows SDK。"
  Write-Warning "已跳过签名（不影响安装包功能）。"
  exit 0
}

# 待签名文件：安装包 + 主程序（若存在）
$targets = @()
if (Test-Path $InstallerPath) { $targets += (Resolve-Path $InstallerPath).Path }
$mainExe = Join-Path $root "release\GoofishMasterDesktop\GoofishMasterDesktop.exe"
if (Test-Path $mainExe) { $targets += $mainExe }

if ($targets.Count -eq 0) {
  Write-Warning "未找到任何待签名文件，跳过。"
  exit 0
}

foreach ($f in $targets) {
  Write-Host "签名中: $f"
  & $st sign /tr $TimestampServer /td sha256 /fd sha256 /sha1 $Thumbprint /v "$f"
  if ($LASTEXITCODE -ne 0) {
    Write-Error "签名失败：$f (exit=$LASTEXITCODE)"
    exit $LASTEXITCODE
  }
  # 验证
  & $st verify /pa "$f"
  if ($LASTEXITCODE -ne 0) {
    Write-Error "签名验证失败：$f"
    exit $LASTEXITCODE
  }
  Write-Host "✓ 已签名并验证：$f"
}

Write-Host "全部文件签名完成。"
