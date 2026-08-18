#Requires -Version 5.1
<#
.SYNOPSIS
  User-space PerfectVoice install (CUDA engine + WI panel). No admin.

.DESCRIPTION
  Copies a PyInstaller onedir to
    %LOCALAPPDATA%\PerfectVoice\engine\perfectvoice-engine.exe
  and the panel to the per-user Resolve Workflow Integration Plugins dir.

  Frozen §3.8 / PR 02 IPC: token-file or stdin, bind 127.0.0.1, no --token-fd 3.
  protocol_version = 1. Does not bundle Demucs/DFN weights.
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

function Test-ForbiddenRoot {
    param([string]$PathValue)
    if (-not $PathValue) { return $false }
    $full = [System.IO.Path]::GetFullPath($PathValue)
    $blocked = @(
        ${env:ProgramFiles},
        ${env:ProgramFiles(x86)},
        $env:ProgramData,
        "C:\Program Files",
        "C:\Program Files (x86)",
        "C:\ProgramData"
    ) | Where-Object { $_ }
    foreach ($b in $blocked) {
        $prefix = [System.IO.Path]::GetFullPath($b).TrimEnd("\")
        if ($full.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Find-WeightFiles {
    param([string]$Root)
    if (-not (Test-Path -LiteralPath $Root)) { return @() }
    $hits = @()
    foreach ($g in $Script:WeightGlobs) {
        $hits += @(Get-ChildItem -LiteralPath $Root -Recurse -File -Filter $g -ErrorAction SilentlyContinue)
    }
    return $hits
}

function Assert-NoWeights {
    param([string]$Root, [string]$Label)
    $hits = Find-WeightFiles -Root $Root
    if ($hits.Count -gt 0) {
        [Console]::Error.WriteLine("refusing: model weights in $Label (installer must not bundle Demucs/DFN):")
        foreach ($h in $hits) { [Console]::Error.WriteLine("  $($h.FullName)") }
        exit 1
    }
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
    $excludeNames = @{
        "WorkflowIntegration.node" = $true
        "install-user.sh"          = $true
        ".gitkeep"                 = $true
        ".DS_Store"                = $true
        "__pycache__"              = $true
    }
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        $name = $_.Name
        if ($excludeNames.ContainsKey($name)) { return }
        if ($name -like "*.test.js") { return }
        foreach ($g in $Script:WeightGlobs) {
            if ($name -like $g) { return }
        }
        $target = Join-Path $Dest $name
        if ($_.PSIsContainer) {
            Copy-TreeFiltered -Source $_.FullName -Dest $target
        } else {
            Copy-Item -LiteralPath $_.FullName -Destination $target -Force
        }
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
    param($Dest, [string]$EngineDirValue, [switch]$AllowStub)

    New-Item -ItemType Directory -Force -Path $Dest.Engine, $Dest.Models, $Dest.Logs, $Dest.Cache, $Dest.Run, $Dest.Panel | Out-Null

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
        Copy-TreeFiltered -Source $EngineDirValue -Dest $Dest.Engine
        Write-Info "staged engine from onedir: $EngineDirValue"
    } elseif ($AllowStub) {
        Write-EngineStubNote -DestDir $Dest.Engine
        Write-Info "staged engine stub (pass -EngineDir for a CUDA onedir)"
    } else {
        Fail-Policy "refusing: -EngineDir is required for a real install (PyInstaller onedir with $Script:EngineName)"
    }

    $repo = Get-RepoRoot
    $panelSrc = Join-Path $repo "host\com.perfectvoice.panel"
    if (-not (Test-Path -LiteralPath (Join-Path $panelSrc "manifest.xml"))) {
        Fail-Policy "panel source missing manifest.xml: $panelSrc"
    }
    Copy-TreeFiltered -Source $panelSrc -Dest $Dest.Panel

    $nodeInPanel = Join-Path $Dest.Panel "WorkflowIntegration.node"
    if (Test-Path -LiteralPath $nodeInPanel) {
        Fail-Policy "refusing: WorkflowIntegration.node must be copied from the host Resolve, not bundled"
    }
    Assert-NoWeights -Root $Dest.Engine -Label "staged engine"
    Assert-NoWeights -Root $Dest.Panel -Label "staged panel"

    if (-not $AllowStub) {
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

if (-not $DryRun -and -not $StageRoot -and (Test-IsAdministrator)) {
    Fail-Policy "refusing: will not install as Administrator (user-space only; no Program Files, no admin-owned %LOCALAPPDATA%). run as the editor account." 2
}

$stageForDryRun = $false
if ($DryRun -and -not $StageRoot) {
    $StageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("pv-win-" + [guid]::NewGuid().ToString("N"))
    $stageForDryRun = $true
}

$dest = Get-Destinations -Root $StageRoot

if ((Test-ForbiddenRoot $dest.Engine) -or (Test-ForbiddenRoot $dest.Panel)) {
    Fail-Policy "refusing: destination is under Program Files / ProgramData." 2
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
    Install-Payload -Dest $dest -EngineDirValue $EngineDir -AllowStub:($DryRun -or [bool]$StageRoot)
} catch {
    if ($stageForDryRun -and $StageRoot -and (Test-Path -LiteralPath $StageRoot)) {
        Remove-Item -LiteralPath $StageRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    throw
}

if (-not $DryRun -and -not $StageRoot) {
    if (-not (Test-VcRedist)) {
        Write-Info "WARNING: Microsoft Visual C++ 2015-2022 (x64) not detected."
        Write-Info "Install https://aka.ms/vs/17/release/vc_redist.x64.exe before launching the CUDA engine."
    }
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $smi) {
        Write-Info "WARNING: nvidia-smi not on PATH. v1.1 expects an NVIDIA driver >= 560 (CUDA 12.6 / cu126)."
    }
}

if ($DryRun) {
    Write-Info "dry-run staged under: $StageRoot"
    Write-Info "dry-run OK"
    if ($stageForDryRun) {
        Remove-Item -LiteralPath $StageRoot -Recurse -Force
    }
    exit 0
}

Write-Info "installed user-space:"
Write-Info "  $($dest.Engine)\$Script:EngineName"
Write-Info "  $($dest.Panel)"
Write-Info "Restart DaVinci Resolve Studio → Workspace → Workflow Integrations → PerfectVoice."
Write-Info "enginePath: $($dest.Engine)\$Script:EngineName"
Write-Info "If the panel is still a macOS-first build, set PERFECTVOICE_ENGINE to that absolute path."
exit 0
