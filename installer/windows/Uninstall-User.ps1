#Requires -Version 5.1
# Thin wrapper: same destinations as Install-User.ps1 -Uninstall.
[CmdletBinding()]
param([switch]$Purge)
$install = Join-Path $PSScriptRoot "Install-User.ps1"
if ($Purge) {
    & $install -Uninstall -Purge
} else {
    & $install -Uninstall
}
exit $LASTEXITCODE
