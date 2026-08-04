<#
.SYNOPSIS
    Bring up the A19 two-VM gate: the hub serving the kiosk site, the panel
    rendering it, both on one Internal switch at known addresses.

.DESCRIPTION
    Three stages, run IN ORDER, from an elevated shell. They are separate
    commands rather than one because the two installs must not overlap:
    measured 2026-08-04, two VMs installing at once on one host and one disk
    roughly DOUBLED the wall's install time (~45-60 min alone became ~2h10m).

        .\vmtest\Start-A19Gate.ps1 -Stage Lab      # the switch + the host's address
        .\vmtest\Start-A19Gate.ps1 -Stage Hub      # recreate + start the hub;  ~20-30 min
        .\vmtest\Start-A19Gate.ps1 -Stage Panel    # recreate + start the panel; ~45-60 min

    THE MACS ARE NOT PARAMETERS. Each build writes `a19-lab.env` into its
    OUT_DIR recording the MAC addresses its netplan matches on; this script
    reads that file. Typing them in both places is the drift that produces a VM
    which installs perfectly and then has no address on the gate's switch —
    which reads as "the hub is down" from the panel and as nothing at all from
    the host.

    WHAT EACH VM GETS: two NICs. The Default Switch leg exists ONLY because the
    installer must fetch 40 packages (§3 wants a network with no internet; apt
    disagrees). The Internal leg is the gate's network — static, no DHCP, no
    route off it.

    The hub is deliberately created with NO extra VHDXs: A19's assertion is that
    `library-mounted` and `backup-drive-mounted` report RED, and they must be red
    for real rather than simulated.

.PARAMETER Stage
    Lab | Hub | Panel. Run them in that order.

.PARAMETER HubOutDir
.PARAMETER WallOutDir
    The build directories the ISOs and their a19-lab.env manifests live in.

.NOTES
    Elevation required. Follow-on verification is in
    Personal\homelab\WALL_PANEL_BRINGUP_PLAN.md §3, steps 3-7.
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Lab', 'Hub', 'Panel')]
    [string]$Stage,

    [string]$HubOutDir  = 'D:\vmtest-out-hub-a19',
    [string]$WallOutDir = 'D:\vmtest-out-wall-a19',

    [string]$SwitchName = 'A19-Lab',
    [string]$VMRoot     = 'D:\HyperV',
    [switch]$Force,

    # Launch Watch-VmConsole.ps1 in its own window for this stage's VM. Worth
    # having: the install is unattended and has no network for most of its life,
    # so the thumbnail is the only view of it.
    [switch]$Watch
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script needs an elevated (Run as Administrator) PowerShell session."
}

function Read-LabManifest {
    param([string]$OutDir, [string]$ExpectRole)
    $f = Join-Path $OutDir 'a19-lab.env'
    if (-not (Test-Path -LiteralPath $f)) {
        throw "No $f. That ISO was built WITHOUT the A19 lab knobs, so its netplan still " +
              "DHCPs a single `e*` NIC and it would come up with no address on '$SwitchName'. " +
              "Rebuild it with SIM_LAB_WAN_MAC / SIM_LAB_MAC / SIM_LAB_ADDR set - see " +
              "vmtest/README.md, 'the A19 two-VM lab'."
    }
    $m = @{}
    foreach ($line in Get-Content -LiteralPath $f) {
        if ($line -match '^\s*(A19_[A-Z_]+)=(.*)$') { $m[$Matches[1]] = $Matches[2].Trim() }
    }
    foreach ($k in 'A19_ROLE', 'A19_WAN_MAC', 'A19_LAB_MAC', 'A19_LAB_ADDR') {
        if (-not $m[$k]) { throw "$f is missing $k - it is truncated or from an older builder." }
    }
    if ($m['A19_ROLE'] -ne $ExpectRole) {
        throw "$f says A19_ROLE=$($m['A19_ROLE']), but this stage is building the $ExpectRole. " +
              "The two OUT_DIRs are swapped - and both seeds are labelled CIDATA, so nothing " +
              "downstream would tell you: the wrong one simply installs the wrong machine."
    }
    return $m
}

function Start-Watcher {
    param([string]$Name, [string]$OutDir)
    $watchDir = Join-Path $OutDir 'watch'
    # Its own window, so this shell stays usable and the watcher survives it.
    # Elevation is inherited, which is the whole reason it is started from here:
    # an unelevated session cannot restart one that died.
    Start-Process -FilePath 'powershell.exe' -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit', '-File',
        (Join-Path $here 'Watch-VmConsole.ps1'),
        '-VMName', $Name, '-OutDir', $watchDir, '-MaxHours', '4', '-UntilOff'
    ) | Out-Null
    Write-Host "  watcher: $watchDir\latest.png  (status: $watchDir\watch-status.txt)" -ForegroundColor Cyan
}

function Assert-Iso {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "No ISO at $Path. Build it first (see vmtest/README.md); this script never builds."
    }
    $iso = Get-Item -LiteralPath $Path
    Write-Host ("  ISO: {0}  ({1:N1} GB, built {2})" -f $iso.FullName, ($iso.Length / 1GB), $iso.LastWriteTime) -ForegroundColor Cyan
    return $iso.FullName
}

switch ($Stage) {

'Lab' {
    & (Join-Path $here 'New-A19Lab.ps1') -SwitchName $SwitchName -WhatIf:$WhatIfPreference
    if ($WhatIfPreference) { return }
    Write-Host ""
    Write-Host "NEXT: .\vmtest\Start-A19Gate.ps1 -Stage Hub" -ForegroundColor Yellow
}

'Hub' {
    $m   = Read-LabManifest -OutDir $HubOutDir -ExpectRole 'hub'
    $iso = Assert-Iso (Join-Path $HubOutDir 'repacked.iso')

    Write-Host "Recreating HomeHub-VMTest on '$SwitchName' at $($m['A19_LAB_ADDR'])" -ForegroundColor Cyan
    # Repacked ISO: it is BOTH the install medium and the seed, so it is passed
    # as both and the second DVD is skipped. Zero keypress, no GRUB edit.
    & (Join-Path $here 'New-HomeHubVm.ps1') `
        -VMName 'HomeHub-VMTest' -VMPath (Join-Path $VMRoot 'HomeHub-VMTest') `
        -UbuntuIsoPath $iso -SeedIsoPath $iso -SkipSecondDvd `
        -MemoryGB 8 -CPUCount 4 -DiskGB 64 `
        -SwitchName 'Default Switch' `
        -LabSwitchName $SwitchName -WanMac $m['A19_WAN_MAC'] -LabMac $m['A19_LAB_MAC'] `
        -Force:$Force -WhatIf:$WhatIfPreference

    if ($WhatIfPreference) { return }
    if ($PSCmdlet.ShouldProcess('HomeHub-VMTest', 'Start')) { Start-VM -Name 'HomeHub-VMTest' }
    if ($Watch) { Start-Watcher -Name 'HomeHub-VMTest' -OutDir $HubOutDir }

    Write-Host ""
    Write-Host "Hub installing. ~20-30 min unattended, then firstboot pulls/loads containers." -ForegroundColor Green
    Write-Host "  ssh hub@$(($m['A19_LAB_ADDR'] -split '/')[0])   (key: $HubOutDir\ssh\homehub-vmtest-ed25519)" -ForegroundColor Green
    Write-Host ""
    Write-Host "DO NOT start the panel until this one is done - two installs at once" -ForegroundColor Yellow
    Write-Host "roughly doubled the wall's install time on 2026-08-04." -ForegroundColor Yellow
    Write-Host "NEXT (after the hub answers ssh and its containers are up):" -ForegroundColor Yellow
    Write-Host "  .\vmtest\Start-A19Gate.ps1 -Stage Panel" -ForegroundColor Yellow
}

'Panel' {
    $m   = Read-LabManifest -OutDir $WallOutDir -ExpectRole 'panel'
    $iso = Assert-Iso (Join-Path $WallOutDir 'wall-repacked.iso')

    # The hub has to be answering BEFORE the panel finishes installing, or the
    # first thing the shell does is fail to resolve its origin - the exact state
    # the last two gates ended in. Checked here rather than discovered later.
    $hubAddr = $null
    $hubManifest = Join-Path $HubOutDir 'a19-lab.env'
    if (Test-Path -LiteralPath $hubManifest) {
        foreach ($line in Get-Content -LiteralPath $hubManifest) {
            if ($line -match '^\s*A19_LAB_ADDR=(.*)$') { $hubAddr = ($Matches[1].Trim() -split '/')[0] }
        }
    }
    if ($hubAddr) {
        # .NET Ping rather than Test-Connection: the parameter that names the
        # target was renamed between Windows PowerShell 5.1 (-ComputerName) and
        # PowerShell 7 (-TargetName), and this script has to run under both.
        $reachable = $false
        try {
            $reachable = (New-Object System.Net.NetworkInformation.Ping).Send($hubAddr, 1500).Status -eq 'Success'
        } catch { $reachable = $false }
        if ($reachable) {
            Write-Host "  hub at $hubAddr answers ICMP." -ForegroundColor Green
        } else {
            Write-Host "WARNING: the hub at $hubAddr does not answer on '$SwitchName'." -ForegroundColor Yellow
            Write-Host "The panel will install fine and then end at ERR_NAME_NOT_RESOLVED," -ForegroundColor Yellow
            Write-Host "which is where the last two gates stopped. Finish -Stage Hub first." -ForegroundColor Yellow
        }
    }

    Write-Host "Recreating Wall-VMTest on '$SwitchName' at $($m['A19_LAB_ADDR'])" -ForegroundColor Cyan
    & (Join-Path $here 'New-HomeHubVm.ps1') `
        -VMName 'Wall-VMTest' -VMPath (Join-Path $VMRoot 'Wall-VMTest') `
        -UbuntuIsoPath $iso -SeedIsoPath $iso -SkipSecondDvd `
        -MemoryGB 4 -CPUCount 2 -DiskGB 32 `
        -SwitchName 'Default Switch' `
        -LabSwitchName $SwitchName -WanMac $m['A19_WAN_MAC'] -LabMac $m['A19_LAB_MAC'] `
        -Force:$Force -WhatIf:$WhatIfPreference

    if ($WhatIfPreference) { return }
    if ($PSCmdlet.ShouldProcess('Wall-VMTest', 'Start')) { Start-VM -Name 'Wall-VMTest' }
    if ($Watch) { Start-Watcher -Name 'Wall-VMTest' -OutDir $WallOutDir }

    $panelAddr = ($m['A19_LAB_ADDR'] -split '/')[0]
    Write-Host ""
    Write-Host "Panel installing. 45-60 min: Subiquity runs each of the 40 packages: entries" -ForegroundColor Green
    Write-Host "as its own curtin system-install, and 23 of them are Electron runtime libs." -ForegroundColor Green
    Write-Host "  ssh panel@$panelAddr   (key: $WallOutDir\ssh\homehub-vmtest-ed25519)" -ForegroundColor Green
    Write-Host ""
    Write-Host "Then verify - WALL_PANEL_BRINGUP_PLAN.md §3 steps 3-7:" -ForegroundColor Yellow
    Write-Host "  journalctl -t wall-kiosk --no-pager | tail -30" -ForegroundColor Yellow
    Write-Host "  sudo -u panel env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 grim /tmp/panel.png" -ForegroundColor Yellow
}

}
