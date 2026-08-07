<#
.SYNOPSIS
    Create (or remove) the A19 gate's Internal Hyper-V switch and give the host
    an address on it.

.DESCRIPTION
    A19 is the two-VM gate: the hub serves the kiosk site and the panel renders
    it. Three things make that not-just-boot-two-VMs, and this script is the
    first of them.

    THE DEFAULT SWITCH WILL NOT DO. It is NAT with quasi-random leases, and the
    kiosk site's guard is `remote_ip {$PANEL_IP}/32` — a single address, which
    is meaningless if the panel's address moves. So: an INTERNAL switch (no
    uplink, no DHCP server, host + guests only) and static addressing on both
    VMs, baked into their ISOs by the builder's SIM_LAB_* knobs.

    The host gets an address on the same subnet for one reason worth stating:
    it is how you reach both VMs. Hyper-V reports no guest IP for Ubuntu Server
    (no KVP daemon), WSL2 cannot route to Hyper-V switch subnets, and hunting
    the NAT with Get-NetNeighbor is what the last two gates spent time on. With
    this switch, both addresses are known before either VM has booted.

    Idempotent, and it READS BACK what it wrote — a switch that exists with the
    wrong address is worse than none, because everything downstream then fails
    somewhere else.

.PARAMETER SwitchName
    Internal switch name. Must match what New-HomeHubVm.ps1 -LabSwitchName gets.

.PARAMETER HostAddress
    The host's address on the lab subnet. NOT a gateway — nothing routes off
    this switch, deliberately: §3 wants a network with no internet, and each VM
    keeps a Default Switch leg for the install's apt traffic.

.PARAMETER Remove
    Tear the switch down. Removing it while a VM is attached leaves that VM's
    adapter disconnected rather than failing, so stop the VMs first.

.EXAMPLE
    .\vmtest\New-A19Lab.ps1 -WhatIf
    .\vmtest\New-A19Lab.ps1

.NOTES
    Elevation required (every Hyper-V cmdlet here needs it).
    The addressing plan lives in one place — vmtest/README.md's A19 section —
    and this script's defaults are that plan.
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$SwitchName  = 'A19-Lab',
    [string]$HostAddress = '10.99.7.1',
    [ValidateRange(8, 30)]
    [int]$PrefixLength   = 24,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script needs an elevated (Run as Administrator) PowerShell session."
}
if (-not (Get-Command Get-VMSwitch -ErrorAction SilentlyContinue)) {
    throw "Hyper-V PowerShell module not found. See vmtest/README.md 'Enable Hyper-V'."
}

$adapterName = "vEthernet ($SwitchName)"

if ($Remove) {
    $sw = Get-VMSwitch -Name $SwitchName -ErrorAction SilentlyContinue
    if (-not $sw) {
        Write-Host "No switch named '$SwitchName' - nothing to remove." -ForegroundColor Yellow
        return
    }
    $attached = Get-VMNetworkAdapter -VMName * -ErrorAction SilentlyContinue |
        Where-Object { $_.SwitchName -eq $SwitchName }
    if ($attached) {
        Write-Host "Still attached to: $(($attached.VMName | Sort-Object -Unique) -join ', ')" -ForegroundColor Yellow
        Write-Host "Those adapters will be left DISCONNECTED, not removed." -ForegroundColor Yellow
    }
    if ($PSCmdlet.ShouldProcess($SwitchName, 'Remove Internal switch')) {
        Remove-VMSwitch -Name $SwitchName -Force -Confirm:$false
        Write-Host "Removed '$SwitchName'." -ForegroundColor Green
    }
    return
}

$sw = Get-VMSwitch -Name $SwitchName -ErrorAction SilentlyContinue
if ($sw) {
    if ($sw.SwitchType -ne 'Internal') {
        throw "A switch named '$SwitchName' exists but is $($sw.SwitchType), not Internal. " +
              "An External switch would put the gate VMs on the house LAN, and a Private one " +
              "would cut the host off from both. Rename or remove it first."
    }
    Write-Host "Switch '$SwitchName' already exists (Internal)." -ForegroundColor Cyan
} elseif ($PSCmdlet.ShouldProcess($SwitchName, 'Create Internal switch (no uplink, no DHCP)')) {
    New-VMSwitch -Name $SwitchName -SwitchType Internal | Out-Null
    Write-Host "Created Internal switch '$SwitchName'." -ForegroundColor Green
}

if ($WhatIfPreference -and -not $sw) {
    Write-Host "WhatIf: would then set $HostAddress/$PrefixLength on '$adapterName'." -ForegroundColor Cyan
    return
}

$adapter = Get-NetAdapter -Name $adapterName -ErrorAction SilentlyContinue
if (-not $adapter) {
    throw "The switch exists but Windows has no '$adapterName' adapter yet. " +
          "Hyper-V creates it asynchronously; re-run this script."
}

# Drop any address we (or a previous plan) left behind, then set ours. Not a
# blanket wipe: only IPv4 on this one adapter, which nothing else uses.
$existing = Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
foreach ($ip in $existing) {
    if ($ip.IPAddress -eq $HostAddress -and $ip.PrefixLength -eq $PrefixLength) { continue }
    if ($PSCmdlet.ShouldProcess("$($ip.IPAddress)/$($ip.PrefixLength) on $adapterName", 'Remove stale address')) {
        Remove-NetIPAddress -InputObject $ip -Confirm:$false
    }
}
if (-not ($existing | Where-Object { $_.IPAddress -eq $HostAddress -and $_.PrefixLength -eq $PrefixLength })) {
    if ($PSCmdlet.ShouldProcess("$HostAddress/$PrefixLength on $adapterName", 'Set host address')) {
        # No -DefaultGateway on purpose: nothing routes off this switch.
        New-NetIPAddress -InterfaceIndex $adapter.ifIndex -IPAddress $HostAddress `
            -PrefixLength $PrefixLength | Out-Null
    }
}

# Private, not Public: a Public profile is the one Windows Firewall treats as
# hostile, and the gate drives ssh/curl from the host into both VMs.
$netProfile = Get-NetConnectionProfile -InterfaceIndex $adapter.ifIndex -ErrorAction SilentlyContinue
if ($netProfile -and $netProfile.NetworkCategory -ne 'Private') {
    if ($PSCmdlet.ShouldProcess($adapterName, "Set network category Private (was $($netProfile.NetworkCategory))")) {
        Set-NetConnectionProfile -InterfaceIndex $adapter.ifIndex -NetworkCategory Private
    }
}

if ($WhatIfPreference) { return }

# READ IT BACK. An address that is set and never verified is the same class of
# bug as an `exit 0` after a failed copy.
$actual = Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -eq $HostAddress -and $_.PrefixLength -eq $PrefixLength }
if (-not $actual) {
    throw "Set $HostAddress/$PrefixLength on '$adapterName' but reading it back found: " +
          (((Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4).IPAddress) -join ', ')
}

Write-Host ""
Write-Host "A19 lab ready." -ForegroundColor Green
Write-Host "  switch : $SwitchName (Internal - no uplink, no DHCP server)" -ForegroundColor Green
Write-Host "  host   : $HostAddress/$PrefixLength on '$adapterName'" -ForegroundColor Green
Write-Host ""
Write-Host "Nothing on this switch has a DHCP server or a route off it. Both VMs" -ForegroundColor Cyan
Write-Host "configure their lab leg statically from the netplan their ISO was built" -ForegroundColor Cyan
Write-Host "with (SIM_LAB_* in vmtest/lib/common.sh), and keep a Default Switch leg" -ForegroundColor Cyan
Write-Host "for the installer's apt traffic." -ForegroundColor Cyan
