#Requires -Version 5.1
<#
.SYNOPSIS
  Type into a Hyper-V VM's console through the synthetic keyboard, for guests
  that have no network yet.

.DESCRIPTION
  Generalised from the ad-hoc `console-driver.ps1` that drove the 2026-08-03/04
  wall sessions out of `.out-wall\` (gitignored, so it was one session away from
  being lost). Same mechanism — `Msvm_Keyboard` via WMI — with the queue file
  and the 90-minute polling loop removed, because the gate needs a handful of
  keystrokes at a known moment, not an interactive terminal.

  WHY THIS EXISTS AT ALL. The one thing a Hyper-V guest running Ubuntu Server
  cannot be reached by is the network, until the network exists: no KVP daemon,
  no guest agent, and during the GRUB menu no kernel. The synthetic keyboard is
  the only channel that works before that, and the GRUB menu is exactly where
  the install gate has to make a choice.

  UNELEVATED IS ENOUGH for `Msvm_Keyboard` on a VM the caller can see, which is
  why this is a separate script from the gate: it can be tested on its own
  without an elevated shell.

.PARAMETER VMName
  The VM to type into.

.PARAMETER Keys
  Directives, in order. Case-insensitive:
    DOWN / UP / LEFT / RIGHT / ENTER / ESC / TAB / SPACE / F1..F12
    CTRL+<c>          e.g. CTRL+c
    WAIT:<seconds>    pause, fractional allowed (WAIT:0.4)
    TEXT:<string>     type the string literally, no Enter
    anything else     typed literally, no Enter

.PARAMETER DryRun
  Resolve the keyboard and print what would be sent. Types nothing.

.EXAMPLE
  # Select the third GRUB entry and boot it
  .\Send-VmConsoleKeys.ps1 -VMName HomeHub-Gate -Keys DOWN,DOWN,ENTER

.NOTES
  Sending keys is not proof they were received — nothing here can read the
  screen back. Callers must assert the OUTCOME (the guest booted, the port
  opened), never the fact that this returned. `Watch-VmConsole.ps1` captures
  the screen if you need to see what happened.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$VMName,
    [Parameter(Mandatory)][string[]]$Keys,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Virtual-key codes. Named rather than inlined: 0x28 in the middle of a gate
# script is the kind of thing that gets "tidied" into the wrong constant.
$VK = @{
    ENTER = 0x0D; ESC = 0x1B; TAB = 0x09; SPACE = 0x20
    LEFT  = 0x25; UP  = 0x26; RIGHT = 0x27; DOWN = 0x28
    CTRL  = 0x11
}
foreach ($n in 1..12) { $VK["F$n"] = 0x6F + $n }   # VK_F1 = 0x70

# CIM, NOT Get-WmiObject — AND THE REASON IS POWERSHELL 7 (found by running the
# lab, 2026-08-06). PS7 removed the WMI cmdlets, but `Get-WmiObject` still
# RESOLVES: auto-loading finds it in Windows PowerShell's own
# Microsoft.PowerShell.Management and imports that module through the Windows
# PowerShell compatibility session. It then works, in the sense of returning
# something — and everything that crosses that session boundary is SERIALISED,
# so what comes back is a `Deserialized.System.Management.ManagementObject`: a
# property bag with the data and none of the methods.
#
#     Method invocation failed because [Deserialized.System.Management.
#     ManagementObject#root\virtualization\v2\Msvm_ComputerSystem] does not
#     contain a method named 'GetRelated'.
#
# That killed the first real lab run at stage 6, after 25 minutes, one line
# after the VM had been created and started. The failure mode is worth naming
# because it is invisible in review: the code is correct 5.1, the cmdlet is
# found, the object looks right, and only the METHODS are gone.
#
# Get-CimInstance / Get-CimAssociatedInstance / Invoke-CimMethod are native to
# both 5.1 and 7, cross no compatibility boundary, and carry no methods on the
# object to lose — the method call is a separate cmdlet, by design.
#
# Caption='Virtual Machine' because Msvm_ComputerSystem also describes the HOST,
# and a host whose name happened to match would otherwise be handed a keyboard
# to type into.
$vm = Get-CimInstance -Namespace root\virtualization\v2 -ClassName Msvm_ComputerSystem `
    -Filter "ElementName='$VMName' and Caption='Virtual Machine'" -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $vm) {
    throw ("No Hyper-V VM named '$VMName' is visible to this session. If it exists, this " +
           'shell may not be able to see it — Hyper-V objects are visible to Administrators ' +
           'and members of Hyper-V Administrators.')
}

# Msvm_SystemDevice is the association named explicitly rather than left to be
# inferred: a VM has many associated instances and this asks for exactly one
# kind of edge.
$kb = Get-CimAssociatedInstance -InputObject $vm -Association Msvm_SystemDevice `
    -ResultClassName Msvm_Keyboard -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $kb) {
    throw ("VM '$VMName' exposes no Msvm_Keyboard. It must exist and be RUNNING — " +
           'a stopped VM has no synthetic keyboard to attach to.')
}

# send_key NAME ARGS — one Msvm_Keyboard call, and its ReturnValue JUDGED.
#
# The old code discarded every result with `$null =`. The header is right that
# sending a key is not proof it was received — nothing here can read the screen
# back — but a NON-ZERO ReturnValue is proof it was not even sent, and that is a
# different claim worth making. Silently returning "sent 3 directive(s)" after
# three refusals is how a gate ends up waiting an hour for a boot that never
# started.
function Invoke-Keyboard {
    param([string]$Method, [hashtable]$Arguments, [string]$What)
    $r = Invoke-CimMethod -InputObject $kb -MethodName $Method -Arguments $Arguments
    if ($r.ReturnValue -ne 0) {
        throw "Msvm_Keyboard.$Method failed for $What on '$VMName' (ReturnValue $($r.ReturnValue))."
    }
}

foreach ($k in $Keys) {
    $key = $k.Trim()

    if ($key -match '^(?i)WAIT:(?<s>[\d.]+)$') {
        $secs = [double]$Matches['s']
        if ($DryRun) { Write-Host "  would wait $secs s" } else { Start-Sleep -Milliseconds ([int]($secs * 1000)) }
        continue
    }

    if ($key -match '^(?i)CTRL\+(?<c>.)$') {
        $c = [string]$Matches['c']
        $code = [uint32][byte][char]$c.ToUpper()
        if ($DryRun) { Write-Host "  would send CTRL+$c" }
        else {
            Invoke-Keyboard PressKey   @{ KeyCode = [uint32]$VK.CTRL } "CTRL down"
            Invoke-Keyboard TypeKey    @{ KeyCode = $code }            "CTRL+$c"
            Invoke-Keyboard ReleaseKey @{ KeyCode = [uint32]$VK.CTRL } "CTRL up"
        }
    }
    elseif ($VK.ContainsKey($key.ToUpper())) {
        if ($DryRun) { Write-Host "  would send $($key.ToUpper())" }
        else { Invoke-Keyboard TypeKey @{ KeyCode = [uint32]$VK[$key.ToUpper()] } $key.ToUpper() }
    }
    else {
        $text = if ($key -match '^(?i)TEXT:(?<t>.*)$') { $Matches['t'] } else { $key }
        if ($DryRun) { Write-Host "  would type '$text'" }
        else { Invoke-Keyboard TypeText @{ AsciiText = $text } "text '$text'" }
    }

    # Between every directive. The guest's input path is a virtual device on a
    # busy host, and GRUB in particular drops keys sent faster than it redraws;
    # 250 ms was the value the wall sessions settled on empirically.
    if (-not $DryRun) { Start-Sleep -Milliseconds 250 }
}

if (-not $DryRun) { Write-Host "  sent $($Keys.Count) directive(s) to $VMName" -ForegroundColor DarkGray }
