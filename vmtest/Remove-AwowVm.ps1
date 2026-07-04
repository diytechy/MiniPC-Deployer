<#
.SYNOPSIS
    WI-10.18 V3 gate — companion teardown for New-AwowVm.ps1.

.DESCRIPTION
    DELIVERED, NOT RUN (same as New-AwowVm.ps1 — elevation + Hyper-V, Peter's
    call). Stops and removes the named Hyper-V VM. Idempotent: if the VM
    doesn't exist, reports so and exits 0 rather than erroring. Supports
    -WhatIf / -Confirm.

.PARAMETER VMName
    Hyper-V VM name. Default: AWOW-VMTest (matches New-AwowVm.ps1's default).

.PARAMETER KeepDisk
    Detach and keep the VHDX file instead of deleting it.

.PARAMETER VMPath
    Only used to report where the VHDX lived if it's being deleted; the
    script reads the actual disk path from the VM itself, not from this.
    Default: $env:USERPROFILE\HyperV\AWOW-VMTest.

.EXAMPLE
    .\Remove-AwowVm.ps1
.EXAMPLE
    .\Remove-AwowVm.ps1 -KeepDisk -WhatIf
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string]$VMName = 'AWOW-VMTest',
    [switch]$KeepDisk,
    [string]$VMPath = (Join-Path $env:USERPROFILE 'HyperV\AWOW-VMTest')
)

$ErrorActionPreference = 'Stop'

function Assert-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This script needs an elevated (Run as Administrator) PowerShell session. See vmtest/README.md."
    }
}

if (-not (Get-Command Get-VM -ErrorAction SilentlyContinue)) {
    throw "Hyper-V PowerShell module not found - nothing to remove."
}

Assert-Elevated

$vm = Get-VM -Name $VMName -ErrorAction SilentlyContinue
if (-not $vm) {
    Write-Host "VM '$VMName' does not exist - idempotent no-op." -ForegroundColor Yellow
    return
}

$disks = (Get-VMHardDiskDrive -VMName $VMName -ErrorAction SilentlyContinue).Path

if ($vm.State -ne 'Off') {
    if ($PSCmdlet.ShouldProcess($VMName, 'Stop VM (TurnOff)')) {
        Stop-VM -Name $VMName -TurnOff -Confirm:$false -WhatIf:$WhatIfPreference
    }
}

if ($PSCmdlet.ShouldProcess($VMName, 'Remove VM')) {
    Remove-VM -Name $VMName -Confirm:$false -WhatIf:$WhatIfPreference
    Write-Host "VM '$VMName' removed." -ForegroundColor Green
}

if (-not $KeepDisk) {
    foreach ($d in $disks) {
        if (Test-Path -LiteralPath $d) {
            if ($PSCmdlet.ShouldProcess($d, 'Delete VHDX')) {
                Remove-Item -LiteralPath $d -Force -WhatIf:$WhatIfPreference
                Write-Host "Deleted disk: $d" -ForegroundColor Green
            }
        }
    }
} else {
    Write-Host "Kept disk(s): $($disks -join ', ')" -ForegroundColor Cyan
}
