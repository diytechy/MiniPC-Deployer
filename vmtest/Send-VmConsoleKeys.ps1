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

$vm = Get-WmiObject -Namespace root\virtualization\v2 -Class Msvm_ComputerSystem `
    -Filter "ElementName='$VMName'" -ErrorAction SilentlyContinue
if (-not $vm) { throw "No Hyper-V VM named '$VMName' is visible to this session." }

$kbRef = $vm.GetRelated('Msvm_Keyboard') | Select-Object -First 1
if (-not $kbRef) {
    throw ("VM '$VMName' exposes no Msvm_Keyboard. It must exist and be running — " +
           'a stopped VM has no synthetic keyboard to attach to.')
}
$kb = [wmi]$kbRef.Path.Path

foreach ($k in $Keys) {
    $key = $k.Trim()

    if ($key -match '^(?i)WAIT:(?<s>[\d.]+)$') {
        $secs = [double]$Matches['s']
        if ($DryRun) { Write-Host "  would wait $secs s" } else { Start-Sleep -Milliseconds ([int]($secs * 1000)) }
        continue
    }

    if ($key -match '^(?i)CTRL\+(?<c>.)$') {
        $code = [byte][char]([string]$Matches['c']).ToUpper()
        if ($DryRun) { Write-Host "  would send CTRL+$($Matches['c'])" }
        else {
            $null = $kb.PressKey($VK.CTRL)
            $null = $kb.TypeKey($code)
            $null = $kb.ReleaseKey($VK.CTRL)
        }
    }
    elseif ($VK.ContainsKey($key.ToUpper())) {
        if ($DryRun) { Write-Host "  would send $($key.ToUpper())" }
        else { $null = $kb.TypeKey($VK[$key.ToUpper()]) }
    }
    else {
        $text = if ($key -match '^(?i)TEXT:(?<t>.*)$') { $Matches['t'] } else { $key }
        if ($DryRun) { Write-Host "  would type '$text'" }
        else { $null = $kb.TypeText($text) }
    }

    # Between every directive. The guest's input path is a virtual device on a
    # busy host, and GRUB in particular drops keys sent faster than it redraws;
    # 250 ms was the value the wall sessions settled on empirically.
    if (-not $DryRun) { Start-Sleep -Milliseconds 250 }
}

if (-not $DryRun) { Write-Host "  sent $($Keys.Count) directive(s) to $VMName" -ForegroundColor DarkGray }
