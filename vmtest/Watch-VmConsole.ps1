<#
.SYNOPSIS
    Capture a Hyper-V VM's console (thumbnail API -> PNG) on a loop, for as long
    as the install actually takes, and say out loud why it stops.

.DESCRIPTION
    The only way to watch an unattended install that has no network yet. Uses
    Hyper-V's GetVirtualSystemThumbnailImage — no guest agent, works from the
    firmware splash onward — and writes PNGs plus a one-line-per-tick status
    file where an UNELEVATED session can read them.

    THIS FILE EXISTS BECAUSE ITS SCRATCH ANCESTOR GAVE UP FIRST. The 2026-08-03
    session's watcher had a hard 90-minute deadline; the 2026-08-04 wall install
    took ~2h10m (two VMs on one host and one disk). It exited quietly, the
    console simply stopped updating, and silence looked exactly like a hung VM.
    So:

      - the default budget is HOURS, not minutes, and it is a parameter;
      - -UntilOff stops on the terminal condition instead of on a clock, which
        is what you actually mean for an install;
      - EVERY exit path writes why it stopped. A watcher that stops silently
        turns "the install is slow" and "the watcher died" into the same
        observation, and an unelevated session cannot restart this one.

.PARAMETER VMName
    The VM to watch.

.PARAMETER OutDir
    Where shots\, latest.png and watch-status.txt land. Created if absent.

.PARAMETER IntervalSeconds
    Seconds between captures. 20 is enough to see Subiquity's phases without
    filling a disk with near-identical PNGs.

.PARAMETER MaxHours
    Upper bound, so a forgotten watcher does not run for a week.

.PARAMETER UntilOff
    Stop once the VM has been Off for two consecutive ticks (an installer
    reboot is not an exit).

.EXAMPLE
    .\vmtest\Watch-VmConsole.ps1 -VMName Wall-VMTest -OutDir D:\vmtest-out-wall-a19\watch

.NOTES
    Elevation required (the thumbnail API is a root\virtualization\v2 call).
    Create OutDir\STOP-WATCHING to stop it from an unelevated shell.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$VMName,

    [Parameter(Mandatory = $true)]
    [string]$OutDir,

    [ValidateRange(5, 600)]
    [int]$IntervalSeconds = 20,

    [ValidateRange(0.25, 24)]
    [double]$MaxHours = 4,

    [switch]$UntilOff,

    [int]$Width  = 1024,
    [int]$Height = 768
)

$ErrorActionPreference = 'Continue'

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script needs an elevated (Run as Administrator) PowerShell session."
}

$shots    = Join-Path $OutDir 'shots'
$status   = Join-Path $OutDir 'watch-status.txt'
$stopFile = Join-Path $OutDir 'STOP-WATCHING'
New-Item -ItemType Directory -Force -Path $shots | Out-Null
Remove-Item -Force $stopFile -ErrorAction SilentlyContinue

Add-Type -AssemblyName System.Drawing

# CIM ONLY. There was a `Get-WmiObject` branch here, preferred because it was
# the form that had actually produced usable PNGs, and guarded by
# `if (Get-Command Get-WmiObject)` on the stated belief that the cmdlet "exists
# in Windows PowerShell 5.1 and NOT in PowerShell 7".
#
# THAT BELIEF IS WRONG, and it cost a lab run on 2026-08-06 through the same
# mechanism in Send-VmConsoleKeys.ps1. PS7 removed the WMI cmdlets, but the name
# still RESOLVES: auto-loading finds it in Windows PowerShell's own
# Microsoft.PowerShell.Management and imports that module through the Windows
# PowerShell compatibility session. So `Get-Command Get-WmiObject` succeeds under
# pwsh, this took the WMI branch, and everything crossing that session boundary
# comes back SERIALISED — a property bag with the data and none of the methods.
# `$vm.GetRelated(...)` then fails with "does not contain a method named
# 'GetRelated'". A fallback selected by a test that is always true is not a
# fallback.
#
# CIM is native to 5.1 and 7 alike, crosses no compatibility boundary, and has
# no methods on the object to lose. Said plainly: this path had never been run
# here when it was written as the fallback, and it is now the only one.
function Save-VmScreenshot {
    param([string]$Name, [string]$Path, [int]$W, [int]$H)

    # Caption='Virtual Machine' because Msvm_ComputerSystem also describes the
    # HOST; Msvm_SettingsDefineState because a VM with checkpoints has several
    # Msvm_VirtualSystemSettingData and only one of them is the running state.
    $vm = Get-CimInstance -Namespace root\virtualization\v2 -ClassName Msvm_ComputerSystem `
            -Filter "ElementName='$Name' and Caption='Virtual Machine'" -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if (-not $vm) { return 'no such VM' }
    $vsd = Get-CimAssociatedInstance -InputObject $vm -Association Msvm_SettingsDefineState `
             -ResultClassName Msvm_VirtualSystemSettingData -ErrorAction SilentlyContinue |
             Select-Object -First 1
    if (-not $vsd) { return 'no virtual system settings' }
    $svc = Get-CimInstance -Namespace root\virtualization\v2 -ClassName Msvm_VirtualSystemManagementService |
             Select-Object -First 1
    $res = Invoke-CimMethod -InputObject $svc -MethodName GetVirtualSystemThumbnailImage -Arguments @{
        TargetSystem = [ciminstance]$vsd
        WidthPixels  = [uint16]$W
        HeightPixels = [uint16]$H
    }
    if ($res.ReturnValue -ne 0 -or -not $res.ImageData) { return "thumbnail rc=$($res.ReturnValue)" }

    # RGB565, 2 bytes per pixel, little-endian.
    $bytes = $res.ImageData
    $bmp  = New-Object System.Drawing.Bitmap($W, $H, [System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
    $rect = New-Object System.Drawing.Rectangle(0, 0, $W, $H)
    $data = $bmp.LockBits($rect, [System.Drawing.Imaging.ImageLockMode]::WriteOnly, $bmp.PixelFormat)
    $row  = New-Object byte[] ($data.Stride)
    for ($y = 0; $y -lt $H; $y++) {
        for ($x = 0; $x -lt $W; $x++) {
            $i = ($y * $W + $x) * 2
            if ($i + 1 -ge $bytes.Length) { break }
            $px = [int]$bytes[$i] -bor ([int]$bytes[$i + 1] -shl 8)
            $r = (($px -shr 11) -band 0x1F); $g = (($px -shr 5) -band 0x3F); $b = ($px -band 0x1F)
            $j = $x * 3
            $row[$j]     = [byte](($b * 255) / 31)
            $row[$j + 1] = [byte](($g * 255) / 63)
            $row[$j + 2] = [byte](($r * 255) / 31)
        }
        [System.Runtime.InteropServices.Marshal]::Copy($row, 0, `
            [IntPtr]($data.Scan0.ToInt64() + ($y * $data.Stride)), $data.Stride)
    }
    $bmp.UnlockBits($data)
    $bmp.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    return 'ok'
}

function Write-Status { param([string]$Text) Add-Content -Path $status -Value $Text }

$deadline = (Get-Date).AddHours($MaxHours)
Write-Status ("{0}  watching {1} -> {2} (every {3}s, budget {4}h{5})" -f `
    (Get-Date -Format 'HH:mm:ss'), $VMName, $shots, $IntervalSeconds, $MaxHours,
    $(if ($UntilOff) { ', stopping when the VM goes Off' } else { '' }))

$i = 0
$offTicks = 0
$reason = $null
while ($true) {
    if (Test-Path $stopFile) { $reason = 'STOP-WATCHING appeared'; break }
    if ((Get-Date) -ge $deadline) {
        $reason = "the ${MaxHours}h budget ran out - the install may STILL BE RUNNING. " +
                  "Re-run with a larger -MaxHours; this is not evidence of a hung VM."
        break
    }

    $i++
    $stamp = Get-Date -Format 'HH:mm:ss'
    try {
        $vm  = Get-VM -Name $VMName -ErrorAction Stop
        $ips = (Get-VMNetworkAdapter -VMName $VMName).IPAddresses -join ','
        $line = "$stamp  state=$($vm.State) cpu=$($vm.CPUUsage)% up=$($vm.Uptime) ip=[$ips]"
        $shot = Join-Path $shots ('{0:d4}.png' -f $i)
        $r = Save-VmScreenshot -Name $VMName -Path $shot -W $Width -H $Height
        $line += "  shot=$r"
        # A stable pointer at the newest capture, so a reader needs no arithmetic.
        if ($r -eq 'ok') { Copy-Item $shot (Join-Path $OutDir 'latest.png') -Force }
        Write-Status $line

        if ($UntilOff) {
            # TWO consecutive Off ticks: Subiquity reboots mid-install, and one
            # sample landing in that window would end the watch at the least
            # useful moment.
            if ($vm.State -eq 'Off') { $offTicks++ } else { $offTicks = 0 }
            if ($offTicks -ge 2) { $reason = 'the VM has been Off for two consecutive ticks'; break }
        }
    } catch {
        # Reported, never fatal: a transient WMI failure must not end a watch
        # that is the only view of a two-hour install.
        Write-Status "$stamp  ERROR: $($_.Exception.Message)"
    }

    Start-Sleep -Seconds $IntervalSeconds
}

Write-Status ("{0}  watcher exiting after {1} tick(s): {2}" -f (Get-Date -Format 'HH:mm:ss'), $i, $reason)
Write-Host "watcher exiting: $reason" -ForegroundColor Yellow
