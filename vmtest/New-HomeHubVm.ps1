<#
.SYNOPSIS
    WI-10.18 V3 gate — create a local Hyper-V Gen2 VM standing in for the AWOW
    AK41, boot the real Ubuntu Server autoinstall ISO + the CIDATA seed ISO
    built by vmtest/build-seed.sh (or the repacked ISO from
    vmtest/build-repacked-iso.sh), and reach compose-up.

.DESCRIPTION
    DELIVERED, NOT RUN. This script requires an elevated PowerShell session and
    the Hyper-V Windows feature — both machine-level changes that are the Owner's
    call, not an agent's. Nobody has executed this script; see docs/status.md
    (WI-10.18) and vmtest/README.md for the exact honest status and the
    step-by-step runbook.

    Idempotent: re-running with the same -VMName is a no-op (reports the
    existing VM and exits 0) unless -Force is passed, in which case the
    existing VM (and, unless -KeepDisk, its VHDX) is removed and recreated.
    Supports -WhatIf / -Confirm via SupportsShouldProcess.

    Defaults are a reasonable stand-in for the AWOW AK41 (Celeron J4125, 4
    cores, 8GB RAM) — not an exact hardware clone, just enough to reach and
    hold compose-up.

.PARAMETER VMName
    Hyper-V VM name. Default: HomeHub-VMTest.

.PARAMETER VMPath
    Directory that holds the VM's config + VHDX. Default:
    $env:USERPROFILE\HyperV\HomeHub-VMTest (created if missing).

.PARAMETER UbuntuIsoPath
    Path to the STOCK Ubuntu Server LTS ISO (unmodified). See
    vmtest/README.md for the download URL + expected SHA256. Mandatory.

.PARAMETER SeedIsoPath
    Path to the CIDATA seed ISO. Either the LIGHT-path seed.iso from
    build-seed.sh (default, attached as a 2nd DVD alongside the stock ISO) or
    the single self-contained ISO from build-repacked-iso.sh (HEAVIER path —
    pass the SAME path for both -UbuntuIsoPath and -SeedIsoPath in that case;
    see -SkipSecondDvd). Mandatory.

.PARAMETER SkipSecondDvd
    Pass this when -UbuntuIsoPath and -SeedIsoPath are the SAME repacked ISO
    (the heavier one-ISO path) so only one DVD drive is attached.

.PARAMETER MemoryGB
    Startup (and, by default, static) memory. Default: 8, matching the AK41's
    8GB. Pass -DynamicMemory to let Windows reclaim idle RAM instead.

.PARAMETER CPUCount
    Virtual processor count. Default: 4, matching the J4125's 4 cores.

.PARAMETER DiskGB
    Dynamic VHDX size cap in GB. Default: 64 (generous vs. the AK41's
    eMMC/SSD — this is a smoke-test stand-in, not a capacity match).

.PARAMETER SwitchName
    Hyper-V virtual switch. Default: 'Default Switch' (Windows' built-in NAT —
    the VM gets internet + can reach the seed/deploy-payload but is NOT
    reachable from other LAN devices, so DNS-client and TLS-from-another-
    device checks in stack/README.md §6 do not apply — see vmtest/README.md).
    Pass an External switch name (Hyper-V Manager -> Virtual Switch Manager ->
    New... -> External, bound to your real NIC) for real LAN exposure — e.g.
    to test split-horizon DNS from an actual LAN client. That switch must
    already exist; this script does not create External switches (that's a
    host-networking change with more blast radius than a VM-local NAT switch,
    left to the Owner's judgement).

.PARAMETER SecureBootTemplate
    Gen2 VMs need Secure Boot set to a Linux-trusted template to boot the
    Ubuntu shim. Default: 'MicrosoftUEFICertificateAuthority' (the template
    Microsoft ships specifically for signed Linux bootloaders — Ubuntu's
    shimx64 is signed under this CA). Pass -DisableSecureBoot instead if you'd
    rather turn Secure Boot off entirely.

.PARAMETER DisableSecureBoot
    Turn Secure Boot off instead of setting -SecureBootTemplate. Not required
    for a stock Ubuntu 24.04 ISO (the CA template above is enough) — offered
    as a fallback if boot fails with Secure Boot on.

.PARAMETER DynamicMemory
    Use Hyper-V dynamic memory (Min 2GB / Startup MemoryGB / Max MemoryGB)
    instead of static. Static (default) more closely mimics the AK41's fixed
    8GB.

.PARAMETER Force
    If a VM named -VMName already exists, stop + remove it (and its VHDX,
    unless -KeepDisk) before recreating. Without -Force, an existing VM is
    left untouched and the script exits 0 (idempotent no-op).

.PARAMETER KeepDisk
    With -Force, keep the existing VHDX file instead of deleting it (still
    detaches it from the removed VM; a fresh VHDX is NOT created in this case
    — the script reattaches the kept one).

.PARAMETER Start
    Start the VM after creation. Default: off — review the VM (and, for the
    LIGHT/seed path, be ready for the one-time GRUB edit in vmtest/README.md)
    before booting it.

.EXAMPLE
    # LIGHT path (default): stock ISO + separate CIDATA seed ISO
    .\New-HomeHubVm.ps1 -UbuntuIsoPath D:\iso\ubuntu-24.04.4-live-server-amd64.iso `
                     -SeedIsoPath   C:\Projects\MiniPC-Deployer\vmtest\.out\seed.iso

.EXAMPLE
    # HEAVIER path: single repacked ISO carries both the OS and the seed
    .\New-HomeHubVm.ps1 -UbuntuIsoPath C:\Projects\MiniPC-Deployer\vmtest\.out\repacked.iso `
                     -SeedIsoPath   C:\Projects\MiniPC-Deployer\vmtest\.out\repacked.iso `
                     -SkipSecondDvd

.EXAMPLE
    # Preview only, no changes
    .\New-HomeHubVm.ps1 -UbuntuIsoPath D:\iso\ubuntu.iso -SeedIsoPath .\vmtest\.out\seed.iso -WhatIf

.NOTES
    Companion teardown: .\Remove-HomeHubVm.ps1
    Full runbook (Hyper-V enable, ISO download+SHA256, GRUB one-time edit,
    what "success" looks like, VM-vs-hardware deltas): vmtest/README.md
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$VMName = 'HomeHub-VMTest',
    [string]$VMPath = (Join-Path $env:USERPROFILE 'HyperV\HomeHub-VMTest'),

    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$UbuntuIsoPath,

    [Parameter(Mandatory = $true)]
    [ValidateScript({ Test-Path -LiteralPath $_ -PathType Leaf })]
    [string]$SeedIsoPath,

    [switch]$SkipSecondDvd,

    [ValidateRange(2, 32)]
    [int]$MemoryGB = 8,

    [ValidateRange(1, 16)]
    [int]$CPUCount = 4,

    [ValidateRange(20, 500)]
    [int]$DiskGB = 64,

    [string]$SwitchName = 'Default Switch',

    # ── A19 two-VM lab (optional; omit all three and nothing below changes) ────
    # The gate needs BOTH VMs on one switch at KNOWN addresses, because the kiosk
    # site's guard is `remote_ip {$PANEL_IP}/32`. An Internal switch has no DHCP,
    # so the guest configures the lab leg statically — and it can only tell the
    # two legs apart by MAC, because both come up as eth0/eth1 in an order VMBus
    # decides. These MACs are therefore not cosmetic: they must match the
    # SIM_LAB_WAN_MAC / SIM_LAB_MAC the ISO was built with, or the installed box
    # has no address on the switch the gate runs over.
    #
    # The Default Switch leg stays, and stays first: the Internal switch has no
    # internet and the installer still has to fetch 40 packages.
    [string]$LabSwitchName,
    [string]$WanMac,
    [string]$LabMac,

    [string]$SecureBootTemplate = 'MicrosoftUEFICertificateAuthority',
    [switch]$DisableSecureBoot,
    [switch]$DynamicMemory,

    [switch]$Force,
    [switch]$KeepDisk,
    [switch]$Start
)

$ErrorActionPreference = 'Stop'

function Assert-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This script needs an elevated (Run as Administrator) PowerShell session. See vmtest/README.md."
    }
}

function Assert-HyperV {
    if (-not (Get-Command Get-VM -ErrorAction SilentlyContinue)) {
        throw "Hyper-V PowerShell module not found. Enable the Hyper-V Windows feature first " + `
              "(machine-level change, needs a reboot) - see vmtest/README.md 'Enable Hyper-V'."
    }
}

Assert-Elevated
Assert-HyperV

# The lab knobs are all-or-nothing. A half-specified lab is the failure this
# whole exercise exists to avoid: the VM comes up, installs cleanly, and simply
# has no address on the switch the gate runs over — which reads as "the hub is
# down" from the panel and as nothing at all from the host.
# The @(...) around the pipeline is load-bearing in Windows PowerShell 5.1:
# Where-Object matching nothing yields $null, and $null.Count is $null, not 0 —
# which would make the -notin test below fire on a build that asked for no lab.
$labKnobs = @(@($LabSwitchName, $WanMac, $LabMac) | Where-Object { $_ })
if ($labKnobs.Count -notin @(0, 3)) {
    throw "-LabSwitchName, -WanMac and -LabMac must be given together (got $($labKnobs.Count) of 3). " +
          "A lab leg with no MAC cannot be matched by the guest's netplan, and a MAC with no lab switch " +
          "pins an adapter that is not there."
}
$LabMode = $labKnobs.Count -eq 3
if ($LabMode) {
    foreach ($m in @($WanMac, $LabMac)) {
        if ($m -notmatch '^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$') {
            throw "'$m' is not a MAC address. Use the same form the ISO was built with (e.g. 00:15:5D:A1:90:10)."
        }
    }
    if (($WanMac -replace '[:-]','') -eq ($LabMac -replace '[:-]','')) {
        throw "-WanMac and -LabMac are the same address. The guest's netplan matches on them; identical MACs make both stanzas claim both NICs."
    }
    if (-not (Get-VMSwitch -Name $LabSwitchName -ErrorAction SilentlyContinue)) {
        throw "No Hyper-V switch named '$LabSwitchName'. Create it first: .\vmtest\New-A19Lab.ps1"
    }
}

$UbuntuIsoPath = (Resolve-Path -LiteralPath $UbuntuIsoPath).Path
$SeedIsoPath   = (Resolve-Path -LiteralPath $SeedIsoPath).Path

$existing = Get-VM -Name $VMName -ErrorAction SilentlyContinue
if ($existing -and -not $Force) {
    Write-Host "VM '$VMName' already exists (state: $($existing.State)) - idempotent no-op." -ForegroundColor Yellow
    Write-Host "Pass -Force to remove and recreate it, or use vmconnect/Hyper-V Manager to inspect it." -ForegroundColor Yellow
    return
}

if ($existing -and $Force) {
    if ($PSCmdlet.ShouldProcess($VMName, 'Stop + remove existing VM (recreate, -Force)')) {
        if ($existing.State -ne 'Off') {
            Stop-VM -Name $VMName -TurnOff -Confirm:$false -WhatIf:$WhatIfPreference
        }
        $existingDisks = @()
        if (-not $KeepDisk) {
            $existingDisks = (Get-VMHardDiskDrive -VMName $VMName -ErrorAction SilentlyContinue).Path
        }
        Remove-VM -Name $VMName -Confirm:$false -WhatIf:$WhatIfPreference
        foreach ($d in $existingDisks) {
            if (Test-Path -LiteralPath $d) {
                Remove-Item -LiteralPath $d -Force -WhatIf:$WhatIfPreference
            }
        }
    }
}

if (-not (Test-Path -LiteralPath $VMPath)) {
    if ($PSCmdlet.ShouldProcess($VMPath, 'Create VM directory')) {
        New-Item -ItemType Directory -Path $VMPath -Force | Out-Null
    }
}

$vhdPath = Join-Path $VMPath "$VMName.vhdx"
if ($KeepDisk -and $existing -and (Test-Path -LiteralPath $vhdPath)) {
    Write-Host "Reusing existing VHDX: $vhdPath" -ForegroundColor Cyan
} elseif ($PSCmdlet.ShouldProcess($vhdPath, "Create dynamic VHDX (${DiskGB}GB cap)")) {
    New-VHD -Path $vhdPath -SizeBytes ([int64]$DiskGB * 1GB) -Dynamic -WhatIf:$WhatIfPreference | Out-Null
}

if ($PSCmdlet.ShouldProcess($VMName, "Create Gen2 VM (${CPUCount} vCPU / ${MemoryGB}GB RAM, switch '$SwitchName')")) {
    New-VM -Name $VMName `
        -Generation 2 `
        -MemoryStartupBytes ([int64]$MemoryGB * 1GB) `
        -VHDPath $vhdPath `
        -Path $VMPath `
        -SwitchName $SwitchName `
        -WhatIf:$WhatIfPreference | Out-Null

    Set-VMProcessor -VMName $VMName -Count $CPUCount -WhatIf:$WhatIfPreference

    if ($DynamicMemory) {
        Set-VMMemory -VMName $VMName -DynamicMemoryEnabled $true `
            -MinimumBytes 2GB -StartupBytes ([int64]$MemoryGB * 1GB) -MaximumBytes ([int64]$MemoryGB * 1GB) `
            -WhatIf:$WhatIfPreference
    } else {
        Set-VMMemory -VMName $VMName -DynamicMemoryEnabled $false -StartupBytes ([int64]$MemoryGB * 1GB) `
            -WhatIf:$WhatIfPreference
    }

    # Secure Boot: Gen2 default template is Windows-only and will refuse to
    # boot the Ubuntu shim. MicrosoftUEFICertificateAuthority is the template
    # Microsoft documents for signed non-Windows (incl. Ubuntu) bootloaders.
    #
    # BY NAME FIRST, BY ID IF THE NAME WILL NOT RESOLVE. Observed 2026-08-04:
    # the identical call set the template on one VM and, twenty minutes later on
    # the same host, failed on the next with "'MicrosoftUEFICertificateAuthority'
    # matches none of the secure boot templates". Nothing about the host had
    # changed, so the name lookup — not the template — is the flaky part. The ID
    # is the same template addressed a way that needs no lookup, so this is one
    # thing tried two ways, NOT a fallback to different behaviour; it still says
    # out loud when it takes the second route.
    if ($DisableSecureBoot) {
        Set-VMFirmware -VMName $VMName -EnableSecureBoot Off -WhatIf:$WhatIfPreference
    } else {
        try {
            Set-VMFirmware -VMName $VMName -EnableSecureBoot On `
                -SecureBootTemplate $SecureBootTemplate -WhatIf:$WhatIfPreference -ErrorAction Stop
        } catch {
            if ($SecureBootTemplate -ne 'MicrosoftUEFICertificateAuthority') { throw }
            Write-Host "  Secure Boot template '$SecureBootTemplate' would not resolve by NAME; retrying by ID." -ForegroundColor Yellow
            Write-Host "    ($($_.Exception.Message))" -ForegroundColor DarkGray
            try {
                Set-VMFirmware -VMName $VMName -EnableSecureBoot On `
                    -SecureBootTemplateId '272e7447-90a4-4563-a4b9-8e4ab00526ce' `
                    -WhatIf:$WhatIfPreference -ErrorAction Stop
                Write-Host "  Secure Boot template set by ID (same template: Microsoft UEFI Certificate Authority)." -ForegroundColor Yellow
            } catch {
                throw "Could not set the Microsoft UEFI CA secure boot template by name OR by id. " +
                      "Ubuntu's shim will not verify under the Windows-only default, so this VM would " +
                      "not boot. Re-run with -DisableSecureBoot to proceed (a recorded delta: the guest " +
                      "then boots unverified), or check `Get-VMHost` on this machine. Underlying error: " +
                      $_.Exception.Message
            }
        }
    }

    # Automatic checkpoints off: this is a disposable smoke-test VM, and
    # production-style checkpoints just burn disk for no benefit here.
    Set-VM -VMName $VMName -AutomaticCheckpointsEnabled $false -WhatIf:$WhatIfPreference

    if ($LabMode) {
        # Pin the WAN leg's MAC (Hyper-V hands out a dynamic one otherwise, and
        # a dynamic MAC changes on recreate — so the netplan that matched it
        # yesterday matches nothing today), then add the lab leg.
        Get-VMNetworkAdapter -VMName $VMName |
            Set-VMNetworkAdapter -StaticMacAddress ($WanMac -replace '[:-]','') -WhatIf:$WhatIfPreference
        Add-VMNetworkAdapter -VMName $VMName -Name 'lab' -SwitchName $LabSwitchName `
            -StaticMacAddress ($LabMac -replace '[:-]','') -WhatIf:$WhatIfPreference
    }

    $ubuntuDvd = Add-VMDvdDrive -VMName $VMName -Path $UbuntuIsoPath -Passthru -WhatIf:$WhatIfPreference
    if (-not $SkipSecondDvd) {
        Add-VMDvdDrive -VMName $VMName -Path $SeedIsoPath -WhatIf:$WhatIfPreference | Out-Null
    }

    # Boot from the Ubuntu ISO's DVD drive first.
    if ($ubuntuDvd) {
        Set-VMFirmware -VMName $VMName -FirstBootDevice $ubuntuDvd -WhatIf:$WhatIfPreference
    }

    Write-Host "VM '$VMName' created." -ForegroundColor Green
    Write-Host "  vCPU/RAM:  $CPUCount / ${MemoryGB}GB $(if ($DynamicMemory) { '(dynamic)' } else { '(static)' })" -ForegroundColor Green
    Write-Host "  Disk:      $vhdPath (dynamic, ${DiskGB}GB cap)" -ForegroundColor Green
    Write-Host "  Switch:    $SwitchName" -ForegroundColor Green
    if ($LabMode) {
        Write-Host "  NIC 1 (wan): $WanMac on '$SwitchName' (DHCP; the installer's apt path)" -ForegroundColor Green
        Write-Host "  NIC 2 (lab): $LabMac on '$LabSwitchName' (static, configured by the ISO's netplan)" -ForegroundColor Green
    }
    Write-Host "  Secure Boot: $(if ($DisableSecureBoot) { 'OFF' } else { "ON ($SecureBootTemplate)" })" -ForegroundColor Green
    Write-Host "  DVD 1 (Ubuntu): $UbuntuIsoPath" -ForegroundColor Green
    if (-not $SkipSecondDvd) {
        Write-Host "  DVD 2 (seed):   $SeedIsoPath" -ForegroundColor Green
    }
    Write-Host ""
    Write-Host "Next: vmtest/README.md - connect via 'vmconnect localhost $VMName' or Hyper-V Manager," -ForegroundColor Cyan
    Write-Host "start the VM, and (LIGHT/seed path only) do the one-time GRUB 'autoinstall' edit." -ForegroundColor Cyan
}

if ($Start) {
    if ($PSCmdlet.ShouldProcess($VMName, 'Start VM')) {
        Start-VM -Name $VMName -WhatIf:$WhatIfPreference
        Write-Host "VM '$VMName' starting - connect now with: vmconnect localhost $VMName" -ForegroundColor Green
    }
}
