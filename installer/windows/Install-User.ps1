#Requires -Version 5.1
<#
.SYNOPSIS
  User-space PerfectVoice install (CUDA engine + WI panel). No admin.

.DESCRIPTION
  Copies a PyInstaller onedir to
    %LOCALAPPDATA%\PerfectVoice\engine\perfectvoice-engine.exe
  and the panel to the per-user Resolve Workflow Integration Plugins dir
    %APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Workflow Integration Plugins\com.perfectvoice.panel\

  Frozen §3.8 / PR 02 IPC: token-file or stdin, bind 127.0.0.1, no --token-fd 3.
  --token-fd is forbidden. protocol_version = 1. Does not bundle Demucs/DFN weights.
  Reinstall wipes dest engine + panel trees (no overlay, no leftover .node / DLLs).
#>
[CmdletBinding()]
param(
    [string]$EngineDir = "",
    [string]$StageRoot = "",
    [switch]$DryRun,
    [switch]$System,
    [switch]$Uninstall,
    [switch]$Purge
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Script:Version = "0.1.0"
$Script:ProtocolVersion = 1
$Script:EngineName = "perfectvoice-engine.exe"
# Frozen §3.8 Windows dests (also built via Join-Path below):
#   %LOCALAPPDATA%\PerfectVoice\engine
#   %LOCALAPPDATA%\PerfectVoice\models
#   %LOCALAPPDATA%\PerfectVoice\Logs
#   %LOCALAPPDATA%\PerfectVoice\Cache
#   %LOCALAPPDATA%\PerfectVoice\run
#   %LOCALAPPDATA%\PerfectVoice\config.json
#   %APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Workflow Integration Plugins\com.perfectvoice.panel
$Script:WeightGlobs = @(
    "*.th", "*.bin", "*.safetensors", "*.ckpt",
    "*.pt", "*.pth", "*.onnx", "*.onnx.data"
)

function Write-Info {
    param([string]$Message)
    Write-Host $Message
}

function Fail-Policy {
    param([string]$Message, [int]$Code = 1)
    [Console]::Error.WriteLine($Message)
    exit $Code
}

function Test-IsAdministrator {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        $p = New-Object Security.Principal.WindowsPrincipal($id)
        return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Get-LocalAppData {
    if ($env:LOCALAPPDATA) { return $env:LOCALAPPDATA }
    return (Join-Path $HOME "AppData\Local")
}

function Get-RoamingAppData {
    if ($env:APPDATA) { return $env:APPDATA }
    return (Join-Path $HOME "AppData\Roaming")
}

function Get-Destinations {
    param([string]$Root)
    if ($Root) {
        $local = Join-Path $Root "LOCALAPPDATA"
        $roaming = Join-Path $Root "APPDATA"
    } else {
        $local = Get-LocalAppData
        $roaming = Get-RoamingAppData
    }
    $pv = Join-Path $local "PerfectVoice"
    return [pscustomobject]@{
        Engine = Join-Path $pv "engine"
        Models = Join-Path $pv "models"
        Logs   = Join-Path $pv "Logs"
        Cache  = Join-Path $pv "Cache"
        Run    = Join-Path $pv "run"
        Config = Join-Path $pv "config.json"
        Panel  = Join-Path $roaming "Blackmagic Design\DaVinci Resolve\Support\Workflow Integration Plugins\com.perfectvoice.panel"
    }
}

function Test-IsPathUnder {
    param([string]$PathValue, [string]$Root)
    if (-not $PathValue -or -not $Root) { return $false }
    $full = [System.IO.Path]::GetFullPath($PathValue).TrimEnd("\")
    $prefix = [System.IO.Path]::GetFullPath($Root).TrimEnd("\")
    if ($full.Equals($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $full.StartsWith(($prefix + "\"), [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-ForbiddenRoot {
    param([string]$PathValue)
    if (-not $PathValue) { return $false }
    $blocked = @(
        ${env:ProgramFiles},
        ${env:ProgramFiles(x86)},
        $env:ProgramData,
        "C:\Program Files",
        "C:\Program Files (x86)",
        "C:\ProgramData"
    ) | Where-Object { $_ }
    foreach ($b in $blocked) {
        if (Test-IsPathUnder -PathValue $PathValue -Root $b) {
            return $true
        }
    }
    return $false
}

function Test-WritesLiveUserProfile {
    param($Dest)
    $local = Get-LocalAppData
    $roaming = Get-RoamingAppData
    return (
        (Test-IsPathUnder -PathValue $Dest.Engine -Root $local) -or
        (Test-IsPathUnder -PathValue $Dest.Panel -Root $roaming)
    )
}

function Find-WeightFiles {
    param([string]$Root)
    if (-not (Test-Path -LiteralPath $Root)) { return @() }
    $hits = @()
    $files = @(Get-ChildItem -LiteralPath $Root -Recurse -File -Force -ErrorAction SilentlyContinue)
    foreach ($f in $files) {
        foreach ($g in $Script:WeightGlobs) {
            if ($f.Name -like $g) {
                $hits += $f
                break
            }
        }
    }
    return $hits
}

function Assert-NoWeights {
    param([string]$Root, [string]$Label)
    $hits = @(Find-WeightFiles -Root $Root)
    if ($hits.Count -gt 0) {
        [Console]::Error.WriteLine("refusing: model weights in $Label (installer must not bundle Demucs/DFN):")
        foreach ($h in $hits) { [Console]::Error.WriteLine("  $($h.FullName)") }
        exit 1
    }
}

function Assert-NoBundledNode {
    param([string]$Root, [string]$Label)
    if (-not (Test-Path -LiteralPath $Root)) { return }
    $hits = @(Get-ChildItem -LiteralPath $Root -Recurse -File -Force -Filter "WorkflowIntegration.node" -ErrorAction SilentlyContinue)
    if ($hits.Count -gt 0) {
        [Console]::Error.WriteLine("refusing: WorkflowIntegration.node must be copied from the host Resolve, not bundled ($Label):")
        foreach ($h in $hits) { [Console]::Error.WriteLine("  $($h.FullName)") }
        exit 1
    }
}

function Reset-DestTree {
    param([string]$PathValue)
    if (Test-Path -LiteralPath $PathValue) {
        Remove-Item -LiteralPath $PathValue -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $PathValue | Out-Null
}

function Test-LooksLikePythonScript {
    param([string]$PathValue)
    $fs = [System.IO.File]::Open($PathValue, "Open", "Read", "ReadWrite")
    try {
        $buf = New-Object byte[] 80
        $n = $fs.Read($buf, 0, 80)
        $head = [System.Text.Encoding]::ASCII.GetString($buf, 0, $n)
    } finally {
        $fs.Close()
    }
    return $head.StartsWith("#!") -and ($head -match "python")
}

function Test-PeHeader {
    param([string]$PathValue)
    $fs = [System.IO.File]::Open($PathValue, "Open", "Read", "ReadWrite")
    try {
        $buf = New-Object byte[] 2
        $n = $fs.Read($buf, 0, 2)
    } finally {
        $fs.Close()
    }
    return ($n -eq 2 -and $buf[0] -eq 0x4D -and $buf[1] -eq 0x5A)
}

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Copy-TreeFiltered {
    param([string]$Source, [string]$Dest)
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    # Junk only. Weights and WorkflowIntegration.node are fail-closed on the
    # source *before* copy — this must not silently strip them.
    $excludeNames = @{
        "install-user.sh" = $true
        ".gitkeep"        = $true
        ".DS_Store"       = $true
        "__pycache__"     = $true
    }
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        $name = $_.Name
        if ($excludeNames.ContainsKey($name)) { return }
        if ($name -like "*.test.js") { return }
        $target = Join-Path $Dest $name
        if ($_.PSIsContainer) {
            Copy-TreeFiltered -Source $_.FullName -Dest $target
        } else {
            Copy-Item -LiteralPath $_.FullName -Destination $target -Force
        }
    }
}

function Assert-DestSubsetOfSource {
    param([string]$Source, [string]$Dest, [string]$Label)
    if (-not (Test-Path -LiteralPath $Dest)) { return }
    $srcRoot = (Resolve-Path -LiteralPath $Source).Path.TrimEnd("\")
    $dstRoot = (Resolve-Path -LiteralPath $Dest).Path.TrimEnd("\")
    $extras = @()
    foreach ($f in @(Get-ChildItem -LiteralPath $Dest -Recurse -File -Force -ErrorAction SilentlyContinue)) {
        $rel = $f.FullName.Substring($dstRoot.Length).TrimStart("\")
        if ($rel -eq "ENGINE-STUB.txt") { continue }
        if ($rel -eq "WorkflowIntegration.node") { continue }
        $srcFile = Join-Path $srcRoot $rel
        if (-not (Test-Path -LiteralPath $srcFile)) {
            $extras += $rel
        }
    }
    if ($extras.Count -gt 0) {
        [Console]::Error.WriteLine("refusing: leftover files in $Label not present in source onedir:")
        foreach ($e in $extras) { [Console]::Error.WriteLine("  $e") }
        exit 1
    }
}

function Get-NodeCandidates {
    $list = @()
    if ($env:ProgramData) {
        $list += (Join-Path $env:ProgramData "Blackmagic Design\DaVinci Resolve\Support\Developer\Workflow Integrations\Examples\SamplePlugin\WorkflowIntegration.node")
    }
    if ($env:ProgramFiles) {
        $list += (Join-Path $env:ProgramFiles "Blackmagic Design\DaVinci Resolve\Developer\Workflow Integrations\Examples\SamplePlugin\WorkflowIntegration.node")
    }
    return $list
}

function Copy-WorkflowNode {
    param([string]$PanelDest)
    foreach ($src in Get-NodeCandidates) {
        if (Test-Path -LiteralPath $src) {
            Copy-Item -LiteralPath $src -Destination (Join-Path $PanelDest "WorkflowIntegration.node") -Force
            Write-Info "copied WorkflowIntegration.node from Resolve Developer examples"
            return
        }
    }
    Write-Info "WARNING: WorkflowIntegration.node not found."
    Write-Info "Install DaVinci Resolve Studio and copy it from Help > Documentation > Developer."
}

function Test-VcRedist {
    $keys = @(
        "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
    )
    foreach ($k in $keys) {
        if (Test-Path -LiteralPath $k) {
            try {
                $inst = (Get-ItemProperty -LiteralPath $k -ErrorAction Stop).Installed
                if ($inst -eq 1) { return $true }
            } catch { }
        }
    }
    return $false
}

function Write-EngineStubNote {
    param([string]$DestDir)
    $text = @"
PerfectVoice engine (Windows v1.1)

enginePath (§3.8 rule 4):
  %LOCALAPPDATA%\PerfectVoice\engine\perfectvoice-engine.exe

This DryRun staged a stub. Production is a PyInstaller onedir built with
the cu126 (CUDA 12.6) torch wheel — see installer/windows/cuda-sku.txt.

Spawn contract (unchanged from PR 02):
  perfectvoice-engine.exe serve --bind 127.0.0.1 --port 0 --token-file <abs>
  READY http://127.0.0.1:<port>
  GET /v1/health + Bearer → {"ok": true, "protocol_version": 1}
  --token-fd is forbidden. Token may also arrive as one stdin line.

Official Demucs weights are NOT in this package. Use Download model
in the panel. Do not install under Program Files / ProgramData.
"@
    Set-Content -LiteralPath (Join-Path $DestDir "ENGINE-STUB.txt") -Value $text -Encoding UTF8
}

function Install-Payload {
    param(
        $Dest,
        [string]$EngineDirValue,
        [switch]$AllowStub,
        [switch]$CopyHostNode
    )

    $repo = Get-RepoRoot
    $panelSrc = Join-Path $repo "host\com.perfectvoice.panel"
    if (-not (Test-Path -LiteralPath (Join-Path $panelSrc "manifest.xml"))) {
        Fail-Policy "panel source missing manifest.xml: $panelSrc"
    }
    Assert-NoWeights -Root $panelSrc -Label "panel source"
    Assert-NoBundledNode -Root $panelSrc -Label "panel source"

    if ($EngineDirValue) {
        if (-not (Test-Path -LiteralPath $EngineDirValue -PathType Container)) {
            Fail-Policy "engine dir not found: $EngineDirValue"
        }
        $exe = Join-Path $EngineDirValue $Script:EngineName
        if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
            Fail-Policy "engine dir missing ${Script:EngineName}: $EngineDirValue"
        }
        Assert-NoWeights -Root $EngineDirValue -Label "EngineDir"
        if (Test-LooksLikePythonScript -PathValue $exe) {
            Fail-Policy "refusing: engine is a Python script (must not require user Python)"
        }
        if (-not (Test-PeHeader -PathValue $exe)) {
            Fail-Policy "refusing: engine is not a PE (need a PyInstaller onedir with ${Script:EngineName})"
        }
    } elseif (-not $AllowStub) {
        Fail-Policy "refusing: -EngineDir is required unless -DryRun (PyInstaller onedir with $Script:EngineName)"
    }

    # Replace dest trees so a second install cannot see a leftover host .node
    # or stale CUDA _internal DLLs from a previous onedir.
    Reset-DestTree -PathValue $Dest.Engine
    Reset-DestTree -PathValue $Dest.Panel
    New-Item -ItemType Directory -Force -Path $Dest.Models, $Dest.Logs, $Dest.Cache, $Dest.Run | Out-Null

    if ($EngineDirValue) {
        Copy-TreeFiltered -Source $EngineDirValue -Dest $Dest.Engine
        Assert-DestSubsetOfSource -Source $EngineDirValue -Dest $Dest.Engine -Label "staged engine"
        Write-Info "staged engine from onedir: $EngineDirValue"
    } else {
        Write-EngineStubNote -DestDir $Dest.Engine
        Write-Info "staged engine stub (pass -EngineDir for a CUDA onedir)"
    }

    Copy-TreeFiltered -Source $panelSrc -Dest $Dest.Panel

    Assert-NoBundledNode -Root $Dest.Panel -Label "staged panel"
    Assert-NoWeights -Root $Dest.Engine -Label "staged engine"
    Assert-NoWeights -Root $Dest.Panel -Label "staged panel"

    if ($CopyHostNode) {
        Copy-WorkflowNode -PanelDest $Dest.Panel
    }
}

function Uninstall-Payload {
    param($Dest)
    foreach ($p in @($Dest.Engine, $Dest.Panel)) {
        if (Test-Path -LiteralPath $p) {
            Remove-Item -LiteralPath $p -Recurse -Force
            Write-Info "removed $p"
        }
    }
    if ($Purge) {
        foreach ($p in @($Dest.Models, $Dest.Logs, $Dest.Cache, $Dest.Run)) {
            if (Test-Path -LiteralPath $p) {
                Remove-Item -LiteralPath $p -Recurse -Force
                Write-Info "purged $p"
            }
        }
        if (Test-Path -LiteralPath $Dest.Config) {
            Remove-Item -LiteralPath $Dest.Config -Force
            Write-Info "purged $($Dest.Config)"
        }
    } else {
        Write-Info "kept models / Cache / Logs / run (pass -Purge to delete)"
    }
}

# --- main ---

if ($System) {
    Fail-Policy "refusing: will not install into Program Files / ProgramData (panel + engine are user-space). omit -System; use the default %LOCALAPPDATA% / %APPDATA% destinations." 2
}

# -EngineDir is required for any real write. -DryRun may stage a stub.
if (-not $DryRun -and -not $EngineDir -and -not $Uninstall) {
    Fail-Policy "refusing: -EngineDir is required unless -DryRun"
}

$liveInstall = (-not $DryRun) -and (-not $StageRoot)

$stageForDryRun = $false
if ($DryRun -and -not $StageRoot) {
    $StageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("pv-win-" + [guid]::NewGuid().ToString("N"))
    $stageForDryRun = $true
}

$dest = Get-Destinations -Root $StageRoot

if ((Test-ForbiddenRoot $dest.Engine) -or (Test-ForbiddenRoot $dest.Panel)) {
    Fail-Policy "refusing: destination is under Program Files / ProgramData." 2
}

# Refuse Administrator on a live user-profile dest (StageRoot remapped
# elsewhere may still run elevated for CI). DryRun temp is not live.
if ((-not $DryRun) -and (Test-IsAdministrator) -and (Test-WritesLiveUserProfile $dest)) {
    Fail-Policy "refusing: will not install as Administrator (user-space only; no Program Files, no admin-owned %LOCALAPPDATA%). run as the editor account." 2
}

Write-Info "PerfectVoice Windows $Script:Version (protocol_version=$Script:ProtocolVersion, sku=cu126)"
Write-Info "engine dest: $($dest.Engine)\$Script:EngineName"
Write-Info "panel dest:  $($dest.Panel)"
Write-Info "models:      $($dest.Models)"
Write-Info "logs:        $($dest.Logs)"
Write-Info "cache:       $($dest.Cache)"
Write-Info "run:         $($dest.Run)"
Write-Info "IPC: serve --bind 127.0.0.1 --port 0 --token-file (or stdin); no --token-fd"

if ($Uninstall) {
    if ($DryRun) {
        Write-Info "dry-run -Uninstall would remove:"
        Write-Info "  $($dest.Engine)"
        Write-Info "  $($dest.Panel)"
        if ($Purge) { Write-Info "  + models / Cache / Logs / run / config.json" }
        Write-Info "dry-run OK"
        if ($stageForDryRun -and (Test-Path -LiteralPath $StageRoot)) {
            Remove-Item -LiteralPath $StageRoot -Recurse -Force
        }
        exit 0
    }
    Uninstall-Payload -Dest $dest
    exit 0
}

try {
    Install-Payload -Dest $dest -EngineDirValue $EngineDir -AllowStub:$DryRun -CopyHostNode:$liveInstall
} catch {
    if ($stageForDryRun -and $StageRoot -and (Test-Path -LiteralPath $StageRoot)) {
        Remove-Item -LiteralPath $StageRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    throw
}

if ($liveInstall) {
    if (-not (Test-VcRedist)) {
        Write-Info "WARNING: Microsoft Visual C++ 2015-2022 (x64) not detected."
        Write-Info "Install https://aka.ms/vs/17/release/vc_redist.x64.exe before launching the CUDA engine."
    }
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $smi) {
        Write-Info "WARNING: nvidia-smi not on PATH. v1.1 expects an NVIDIA driver >= 560 (CUDA 12.6 / cu126)."
    }
    Write-Info "WARNING: Resolve's documented WI plugin scan is %PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Workflow Integration Plugins\."
    Write-Info "This installer will not write ProgramData (no admin). User-space dest is %APPDATA%\…\Workflow Integration Plugins\com.perfectvoice.panel\."
    Write-Info "If Workspace → Workflow Integrations does not list PerfectVoice after restart, copy that folder into the PROGRAMDATA path by hand. Do not re-run this script elevated."
}

if ($DryRun) {
    Write-Info "dry-run staged under: $StageRoot"
    Write-Info "dry-run OK"
    if ($stageForDryRun) {
        Remove-Item -LiteralPath $StageRoot -Recurse -Force
    }
    exit 0
}

$exeOut = Join-Path $dest.Engine $Script:EngineName
Write-Info "installed user-space:"
if (Test-Path -LiteralPath $exeOut) {
    Write-Info "  $exeOut"
} else {
    Write-Info "  $($dest.Engine) (no $Script:EngineName — stub/stage only)"
}
Write-Info "  $($dest.Panel)"
Write-Info "enginePath (§3.8 rule 4): $exeOut"
Write-Info "Restart DaVinci Resolve Studio → Workspace → Workflow Integrations → PerfectVoice."
exit 0
