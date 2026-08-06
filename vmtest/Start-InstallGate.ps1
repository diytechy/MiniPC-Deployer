#Requires -Version 7.0
<#
.SYNOPSIS
  Boot the PRODUCTION install image in Hyper-V and assert what it produced.

.DESCRIPTION
  The gap this closes, in one sentence from the repo's own docs: *"you cannot
  dry-run this ISO to completion in Hyper-V. A virtual disk does not report that
  serial, so the install stops at storage selection."* Every gate before this
  one therefore ran a SIM build — a different user-data, a different disk pin, a
  different .env — and nothing at all was checked after `Start-VM`.

  On 2026-08-06 a production stick installed a bare Ubuntu onto the real hub and
  reported success: no /opt/homehub, no openssh-server, no docker, and no way in
  because the console account is password-locked. Nothing in the repo could have
  caught it, because nothing had ever looked at an installed machine.

  TWO RUNS OF THE SAME IMAGE, asserting opposite properties:

    -Run Containment   Boot the shipped ISO untouched. The pin must find no
                       matching disk, and NOTHING MAY BE WRITTEN. Checked
                       against the artifact — a dynamic VHDX only grows when
                       written to — rather than against the installer's own
                       claim, which is the thing under test.

    -Run Completeness  Boot the derived gate ISO (see make-gate-iso.sh: same
                       payload, one extra unattended-unpinned entry, equivalence
                       asserted at build time), let it install, then SSH in and
                       run assert-installed.sh + healthcheck.sh.

  THE VHDX IS A SECRET ARTIFACT the moment Completeness finishes: the production
  image carries materialised credentials, so the installed disk carries them
  too. It is destroyed on every exit path unless -KeepVhdxForDebug, which says
  so loudly and names the file (SECRET_HANDOFF.md).

  ADDRESS DISCOVERY WITHOUT KVP. These guests run no hv-kvp-daemon, so
  Get-VMNetworkAdapter's .IPAddresses is always empty — the existing gate's
  Watch-VmConsole has an `ip=[]` column that has never once been populated. The
  MAC is knowable from Hyper-V, so this polls the HOST's neighbour table for it
  instead. No guest cooperation required.

.PARAMETER IsoPath
  The production ISO for -Run Containment. For Completeness, the GATE ISO
  derived from it — pass -GateIsoPath, or let it default to <IsoPath>-gate.iso.

.PARAMETER Run
  Containment, Completeness, or Both (default). Both runs Containment first:
  if the pin does not hold there is no point installing.

.PARAMETER KeepVhdxForDebug
  Leave the VM and its disk behind. Prints a warning naming the file, because
  what it leaves behind is credentials.

.NOTES
  NEEDS ADMINISTRATOR (Hyper-V). Reuses New-HomeHubVm.ps1 for VM creation rather
  than reimplementing it — that script already carries the Secure Boot template
  retry and the MAC/lab guards.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$IsoPath,
    [string]$GateIsoPath,
    [ValidateSet('Containment', 'Completeness', 'Both')][string]$Run = 'Both',
    [ValidateSet('hub', 'wall')][string]$Target = 'hub',
    [string]$VMName = 'HomeHub-Gate',
    [string]$VMPath = 'D:\HyperV\HomeHub-Gate',
    [int]$MemoryGB = 8,
    [int]$CPUCount = 4,
    [int]$DiskGB = 64,
    [string]$SwitchName = 'Default Switch',
    [string]$SshUser = 'hub',
    [string]$SshKey,
    # How long to let the pinned install prove it is NOT writing. The halt lands
    # in ~2-4 min; 8 gives margin without making a failing gate slow.
    [int]$ContainmentMinutes = 8,
    # An unattended install plus first boot on 4 vCPU: measured 20-30 min for the
    # hub. 60 is a timeout, not an expectation.
    [int]$InstallTimeoutMinutes = 60,
    [switch]$KeepVhdxForDebug,
    [switch]$DisableSecureBoot,
    [switch]$SkipHealthcheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$VmTestDir = $PSScriptRoot
$script:Failures = @()

function Write-Head { param([string]$m) Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Write-Ok2 { param([string]$m) Write-Host "  PASS  $m" -ForegroundColor Green }
function Write-Bad { param([string]$m) Write-Host "  FAIL  $m" -ForegroundColor Red; $script:Failures += $m }
function Write-Info { param([string]$m) Write-Host "        $m" -ForegroundColor DarkGray }

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator is required: creating and destroying Hyper-V VMs.'
}
if (-not (Test-Path -LiteralPath $IsoPath)) { throw "ISO not found: $IsoPath" }
if (-not $GateIsoPath) { $GateIsoPath = [IO.Path]::ChangeExtension($IsoPath, $null).TrimEnd('.') + '-gate.iso' }

$vhdxPath = Join-Path $VMPath "$VMName.vhdx"

function Remove-GateVm {
    <#
      .SYNOPSIS
        Destroy the VM and its disk. Called on EVERY exit path.

      .DESCRIPTION
        Not tidiness — hygiene. After Completeness the VHDX holds a full
        production install: the site .env, the Samba credentials, the SSH keys,
        every secret Materialize-Deploy put on the stick. Leaving that on D: as
        a side effect of running a test is exactly the accident SECRET_HANDOFF.md
        is about.
    #>
    param([switch]$Quiet)
    if ($KeepVhdxForDebug) {
        Write-Host ''
        Write-Host '  ! -KeepVhdxForDebug: the VM and its disk were NOT destroyed.' -ForegroundColor Yellow
        Write-Host "  ! $vhdxPath" -ForegroundColor Yellow
        Write-Host '  ! That disk carries MATERIALISED CREDENTIALS. Treat it like the DPAPI' -ForegroundColor Yellow
        Write-Host '  ! store (SECRET_HANDOFF.md) and delete it when you are done.' -ForegroundColor Yellow
        return
    }
    try {
        & (Join-Path $VmTestDir 'Remove-HomeHubVm.ps1') -VMName $VMName -ErrorAction SilentlyContinue | Out-Null
    } catch { }
    if (Test-Path -LiteralPath $vhdxPath) {
        try { Remove-Item -LiteralPath $vhdxPath -Force -ErrorAction Stop } catch {
            Write-Host "  ! could not delete $vhdxPath — it holds credentials. Delete it by hand." -ForegroundColor Yellow
        }
    }
    if (-not $Quiet) { Write-Info 'VM and disk destroyed (they carried credentials).' }
}

function New-GateVm {
    param([string]$Iso)
    & (Join-Path $VmTestDir 'New-HomeHubVm.ps1') `
        -VMName $VMName -VMPath $VMPath -UbuntuIsoPath $Iso -SeedIsoPath $Iso -SkipSecondDvd `
        -MemoryGB $MemoryGB -CPUCount $CPUCount -DiskGB $DiskGB -SwitchName $SwitchName `
        -DisableSecureBoot:$DisableSecureBoot -Force | Out-Null
}

function Get-VhdxBytes {
    try { return (Get-VHD -Path $vhdxPath -ErrorAction Stop).FileSize } catch { return -1 }
}

function Resolve-GuestAddress {
    <#
      .SYNOPSIS
        The guest's IPv4, found from the host's neighbour table by MAC.
      .DESCRIPTION
        Get-VMNetworkAdapter's .IPAddresses needs the KVP daemon, which Ubuntu
        Server does not run — the existing Watch-VmConsole prints an `ip=[]`
        column that has never been populated. The MAC is authoritative and comes
        from Hyper-V; ARP is populated by the guest's own DHCP traffic.
      #>
    param([int]$TimeoutMinutes)

    $mac = (Get-VMNetworkAdapter -VMName $VMName | Select-Object -First 1).MacAddress
    if (-not $mac) { return $null }
    $macDash = ($mac -replace '(..)(?=.)', '$1-').ToUpper()
    Write-Info "watching for MAC $macDash in the host neighbour table"

    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    while ((Get-Date) -lt $deadline) {
        $n = Get-NetNeighbor -ErrorAction SilentlyContinue |
            Where-Object { $_.LinkLayerAddress -eq $macDash -and $_.State -in @('Reachable', 'Stale', 'Permanent') -and $_.IPAddress -notmatch ':' } |
            Select-Object -First 1
        if ($n) { return $n.IPAddress }
        Start-Sleep -Seconds 15
    }
    return $null
}

function Invoke-GuestSsh {
    param([string]$Address, [string]$Command, [int]$TimeoutSec = 300)
    $sshArgs = @(
        '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
        '-o', 'LogLevel=ERROR', '-o', "ConnectTimeout=$TimeoutSec"
    )
    if ($SshKey) { $sshArgs += @('-i', $SshKey) }
    $sshArgs += @("$SshUser@$Address", $Command)
    $out = & ssh @sshArgs 2>&1
    return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = ($out -join "`n") }
}

# ══ RUN A — containment ═════════════════════════════════════════════════════
function Invoke-ContainmentRun {
    Write-Head 'Run A — containment: the shipped ISO must refuse to install here'
    Write-Info "ISO: $IsoPath"

    New-GateVm -Iso $IsoPath
    $before = Get-VhdxBytes
    if ($before -lt 0) { Write-Bad 'could not read the VHDX size — nothing can be asserted about writes'; return }
    Write-Info "fresh VHDX: $([math]::Round($before/1MB,1)) MB"

    Start-VM -Name $VMName | Out-Null
    Write-Info "started; letting it run for $ContainmentMinutes minutes"
    Start-Sleep -Seconds ($ContainmentMinutes * 60)

    $after = Get-VhdxBytes
    $grownMB = [math]::Round(($after - $before) / 1MB, 1)
    Write-Info "VHDX now: $([math]::Round($after/1MB,1)) MB (grew $grownMB MB)"

    # 64 MB of slack. A dynamic VHDX moves a little on its own; an install moves
    # gigabytes. Anything between the two is a signal worth failing on rather
    # than a threshold worth tuning.
    if (($after - $before) -lt 64MB) {
        Write-Ok2 "nothing was written: the disk pin held and the install refused this machine"
    } else {
        Write-Bad "the disk GREW by $grownMB MB — the pinned image wrote to a machine it should have refused"
        Write-Info 'the containment pin is the only thing stopping this ISO installing on the wrong box'
    }

    Stop-VM -Name $VMName -TurnOff -Force -ErrorAction SilentlyContinue | Out-Null
}

# ══ RUN B — completeness ════════════════════════════════════════════════════
function Invoke-CompletenessRun {
    Write-Head 'Run B — completeness: the same image must install FULLY'
    if (-not (Test-Path -LiteralPath $GateIsoPath)) {
        Write-Bad "gate ISO not found: $GateIsoPath"
        Write-Info 'build it in WSL first:'
        Write-Info "  bash vmtest/make-gate-iso.sh --src-iso <wsl path to the production ISO>"
        return
    }
    Write-Info "gate ISO: $GateIsoPath"

    New-GateVm -Iso $GateIsoPath
    Start-VM -Name $VMName | Out-Null

    # Select the LAST menu entry — the gate entry make-gate-iso.sh appended.
    # UP from entry 0 WRAPS to the last one, so this does not depend on how many
    # entries the menu has; adding one to the shipped image will not silently
    # start selecting the wrong thing. The wait is for firmware POST before GRUB
    # draws, and the ISO's timeout is 10s, so this has margin at both ends.
    Start-Sleep -Seconds 12
    try {
        & (Join-Path $VmTestDir 'Send-VmConsoleKeys.ps1') -VMName $VMName -Keys 'UP', 'WAIT:0.5', 'ENTER'
        Write-Info 'selected the last GRUB entry (UP wraps to it) and pressed Enter'
    } catch {
        Write-Bad "could not drive the GRUB menu: $($_.Exception.Message)"
        return
    }

    Write-Info "installing; waiting up to $InstallTimeoutMinutes min for the guest to appear on the network"
    $addr = Resolve-GuestAddress -TimeoutMinutes $InstallTimeoutMinutes
    if (-not $addr) {
        Write-Bad "the guest never appeared on the network within $InstallTimeoutMinutes minutes"
        Write-Info 'that is either a failed install or a failed boot; capture the console to tell them apart:'
        Write-Info "  .\vmtest\Watch-VmConsole.ps1 -VMName $VMName -OutDir <dir>"
        return
    }
    Write-Ok2 "guest reachable at $addr"

    # SSH is the first real assertion, not a step. The 2026-08-06 box answered
    # pings for hours with no sshd, so "it responds" and "you can get in" are
    # different claims and only the second is useful.
    $deadline = (Get-Date).AddMinutes(15)
    $up = $false
    while ((Get-Date) -lt $deadline) {
        if ((Invoke-GuestSsh -Address $addr -Command 'true' -TimeoutSec 10).ExitCode -eq 0) { $up = $true; break }
        Start-Sleep -Seconds 20
    }
    if (-not $up) {
        Write-Bad "the guest answers on the network but SSH never came up at $addr"
        Write-Info 'this is the 2026-08-06 signature: reachable, and no way in'
        return
    }
    Write-Ok2 'SSH accepted the operator key'

    # Copy the checkers in rather than assuming the image ships them: the point
    # is to test the image, and an image missing its own scripts should fail the
    # CHECK, not skip it silently.
    $assert = Join-Path $VmTestDir 'assert-installed.sh'
    Write-Info 'running assert-installed.sh on the guest'
    $scp = & scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR `
        @(if ($SshKey) { '-i'; $SshKey }) $assert "${SshUser}@${addr}:/tmp/assert-installed.sh" 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Bad "could not copy assert-installed.sh: $scp"; return }

    $r = Invoke-GuestSsh -Address $addr -Command "bash /tmp/assert-installed.sh --target $Target"
    Write-Host $r.Output
    if ($r.ExitCode -eq 0) { Write-Ok2 'install completeness: all checks passed' }
    else { Write-Bad "install completeness: assert-installed.sh exited $($r.ExitCode)" }

    if ($SkipHealthcheck) {
        Write-Info '-SkipHealthcheck: service health was NOT checked'
        return
    }
    # The stack's own checker, which has existed and been correct all along and
    # has simply never been called by anything.
    Write-Info 'running stack/provision/healthcheck.sh on the guest'
    $h = Invoke-GuestSsh -Address $addr -Command 'sudo bash /opt/homehub/stack/provision/healthcheck.sh 2>&1'
    Write-Host $h.Output
    if ($h.ExitCode -eq 0) { Write-Ok2 'service health: all checks passed' }
    else { Write-Bad "service health: healthcheck.sh exited $($h.ExitCode)" }
}

# ══ drive ═══════════════════════════════════════════════════════════════════
try {
    if ($Run -in @('Containment', 'Both')) { Invoke-ContainmentRun }
    if ($Run -in @('Completeness', 'Both')) {
        # Containment failing means the pin does not hold. Installing after that
        # would produce a green Completeness result on an image that will wipe
        # the wrong machine, which is a worse outcome than stopping.
        if ($script:Failures.Count -gt 0 -and $Run -eq 'Both') {
            Write-Head 'Run B skipped'
            Write-Info 'containment failed; an image that installs where it should refuse is not worth testing further'
        } else {
            Invoke-CompletenessRun
        }
    }
}
finally {
    Remove-GateVm
}

Write-Head 'Gate result'
if ($script:Failures.Count -eq 0) {
    Write-Host '  PASSED — the production image refused the wrong machine, installed completely,' -ForegroundColor Green
    Write-Host '           and came up healthy in a VM.' -ForegroundColor Green
    Write-Info 'It does NOT prove: TLS trust, ACME, OAuth, Cloudflare, Wi-Fi, the CIFS mounts,'
    Write-Info 'the real data drives, thermals, or anything about the target hardware.'
    exit 0
}
Write-Host "  FAILED — $($script:Failures.Count) check(s):" -ForegroundColor Red
$script:Failures | ForEach-Object { Write-Host "    - $_" -ForegroundColor Red }
exit 1
