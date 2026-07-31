@echo off
setlocal EnableExtensions

REM ===========================================================================
REM  Run-V3Gate.cmd - right-click "Run as administrator" launcher for the V3
REM  gate VM. Wraps New-AwowVm.ps1, which needs an elevated session because
REM  Hyper-V's WMI namespace refuses non-admin callers.
REM
REM  Usage:  right-click -> Run as administrator      (or just double-click:
REM          it re-launches itself elevated via UAC)
REM
REM  Optional switches:
REM    /repacked ZERO-KEYPRESS: boot .out\repacked.iso, which already carries
REM              "autoinstall ds=nocloud;s=/cdrom/nocloud/" in its grub.cfg.
REM              No GRUB edit, nothing to type. Build it first with:
REM                bash vmtest/build-repacked-iso.sh --src-iso <stock.iso>
REM    /force    delete an existing AWOW-VMTest VM *and its VHDX* first.
REM              PROMPTS before destroying anything - add /yes to skip that.
REM    /yes      answer the /force confirmation automatically (scripted runs)
REM    /whatif   preview only - creates nothing
REM    /noconn   do not open vmconnect afterwards
REM  Optional 1st positional arg: full path to the stock Ubuntu ISO.
REM
REM  RUNS THE VM ONCE. A VM that is already Running is never restarted, never
REM  recreated and never touched - the script just reports it and connects. The
REM  ONLY thing that rebuilds an existing VM is /force, and that now asks first,
REM  so re-running this command from shell history cannot silently wipe a VM
REM  mid-install. When it is done it returns you to your shell; if it had to
REM  elevate itself into a new window, it leaves that window at an elevated
REM  prompt instead of closing, so you can keep issuing commands there.
REM
REM  NOTE ON EXISTING VMs: New-AwowVm.ps1 is deliberately idempotent - if the
REM  VM already exists it prints a notice, returns, and does NOT start it, with
REM  exit code 0. So checking errorlevel alone is not enough to know the VM is
REM  actually running: this script queries the VM state before and after, and
REM  refuses to claim success it has not verified.
REM
REM  Full runbook: vmtest\README.md
REM ===========================================================================

title AWOW V3 gate - Hyper-V test VM

REM -- 1. elevation: self-elevate if we were merely double-clicked ------------
fltmc >nul 2>&1
if errorlevel 1 (
    echo Not running as administrator - asking Windows to re-launch elevated...
    REM /elevated marks the new window as OURS, so it ends at an interactive
    REM prompt rather than vanishing when you press a key.
    if "%~1"=="" (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '/elevated' -Verb RunAs"
    ) else (
        powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '%* /elevated' -Verb RunAs"
    )
    exit /b 0
)

set "VMTEST_DIR=%~dp0"
if "%VMTEST_DIR:~-1%"=="\" set "VMTEST_DIR=%VMTEST_DIR:~0,-1%"

REM -- 2. parse switches ------------------------------------------------------
set "EXTRA_ARGS="
set "OPEN_CONSOLE=1"
set "UBUNTU_ISO="
set "DO_FORCE="
set "DO_WHATIF="
set "DO_REPACKED="
set "SELF_ELEVATED="
set "DO_YES="

:parseargs
if "%~1"=="" goto :parsed
if /i "%~1"=="/elevated" (set "SELF_ELEVATED=1" & shift & goto :parseargs)
if /i "%~1"=="/yes"      (set "DO_YES=1"        & shift & goto :parseargs)
if /i "%~1"=="/repacked" (set "DO_REPACKED=1" & shift & goto :parseargs)
if /i "%~1"=="/force"  (set "EXTRA_ARGS=%EXTRA_ARGS% -Force"  & set "DO_FORCE=1"  & shift & goto :parseargs)
if /i "%~1"=="/whatif" (set "EXTRA_ARGS=%EXTRA_ARGS% -WhatIf" & set "DO_WHATIF=1" & shift & goto :parseargs)
if /i "%~1"=="/noconn" (set "OPEN_CONSOLE=0"                  & shift & goto :parseargs)
set "UBUNTU_ISO=%~1"
shift
goto :parseargs
:parsed

REM -- 3. defaults ------------------------------------------------------------
REM VHDX goes to D: on purpose - C: is the tight drive on this box.
if "%VM_NAME%"=="" set "VM_NAME=AWOW-VMTest"
if "%VM_PATH%"=="" set "VM_PATH=D:\HyperV\AWOW-VMTest"
if "%SEED_ISO%"=="" set "SEED_ISO=%VMTEST_DIR%\.out\seed.iso"

set "ISO_FROM_DEFAULT="
if "%UBUNTU_ISO%"=="" (
    set "UBUNTU_ISO=D:\iso\ubuntu-24.04.4-live-server-amd64.iso"
    set "ISO_FROM_DEFAULT=1"
)

REM Point releases move on - fall back to whatever server ISO is in D:\iso.
REM ONLY when we picked the default: an ISO path you passed explicitly must
REM fail loudly if it is wrong, never get silently swapped for another image.
if "%ISO_FROM_DEFAULT%"=="1" if not exist "%UBUNTU_ISO%" (
    for %%F in ("D:\iso\ubuntu-*-live-server-amd64.iso") do set "UBUNTU_ISO=%%~fF"
)

REM /repacked: ONE self-contained ISO in both drives, second DVD skipped. Its
REM grub.cfg already carries the autoinstall args, so nothing is typed at boot.
REM An explicit path given on the command line wins over the .out default -
REM that is how you point at a repacked ISO built to another drive.
set "BOOT_MODE=LIGHT - needs the one-time GRUB edit"
if defined DO_REPACKED (
    if defined ISO_FROM_DEFAULT set "UBUNTU_ISO=%VMTEST_DIR%\.out\repacked.iso"
    set "EXTRA_ARGS=%EXTRA_ARGS% -SkipSecondDvd"
    set "BOOT_MODE=REPACKED - zero keypresses"
)
REM Same image in both drives: the repacked ISO carries its own /nocloud seed.
if defined DO_REPACKED set "SEED_ISO=%UBUNTU_ISO%"

echo.
echo ================== AWOW V3 gate - create + start VM ==================
echo   VM name    : %VM_NAME%
echo   Boot mode  : %BOOT_MODE%
echo   Ubuntu ISO : %UBUNTU_ISO%
echo   Seed ISO   : %SEED_ISO%
echo   VM path    : %VM_PATH%
echo ======================================================================
echo.

REM -- 4. pre-flight ----------------------------------------------------------
if not exist "%VMTEST_DIR%\New-AwowVm.ps1" (
    echo ERROR: New-AwowVm.ps1 not found next to this file.
    echo        Expected: %VMTEST_DIR%\New-AwowVm.ps1
    goto :fail
)
if defined DO_REPACKED if not exist "%UBUNTU_ISO%" (
    echo ERROR: repacked ISO not found:
    echo        %UBUNTU_ISO%
    echo        Build it in WSL - takes a few minutes and about 4 GB:
    echo          bash vmtest/build-repacked-iso.sh --src-iso /mnt/d/iso/ubuntu-24.04.4-live-server-amd64.iso
    goto :fail
)
if not exist "%UBUNTU_ISO%" (
    echo ERROR: stock Ubuntu ISO not found:
    echo        %UBUNTU_ISO%
    echo        Download it per vmtest\README.md section 2, or pass the path:
    echo        Run-V3Gate.cmd "D:\path\to\ubuntu-24.04.4-live-server-amd64.iso"
    goto :fail
)
if not exist "%SEED_ISO%" (
    echo ERROR: seed ISO not found:
    echo        %SEED_ISO%
    echo        Build it first in WSL:  bash vmtest/build-seed.sh
    goto :fail
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "if (-not (Get-Command Get-VM -ErrorAction SilentlyContinue)) { exit 1 }; Get-VM | Out-Null; exit 0" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Hyper-V is not usable from this session.
    echo        Enable it once, elevated, then reboot:
    echo          Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V -All
    goto :fail
)

REM -- 5. what is there already? ---------------------------------------------
call :getstate
if defined VMSTATE (
    echo Existing VM found - current state: %VMSTATE%
    echo.
)

if defined DO_WHATIF   goto :createvm
if not defined VMSTATE goto :createvm
if defined DO_FORCE    goto :confirmforce
goto :existing

REM /force is the ONLY path that destroys an existing VM, so make it deliberate.
REM Without this, re-running the command from shell history - easy to do while
REM waiting on an install - silently wipes the VM and starts the whole gate over.
:confirmforce
if defined DO_YES goto :createvm
echo /force will DELETE the VM "%VM_NAME%" and its VHDX under
echo    %VM_PATH%
if /i "%VMSTATE%"=="Running" echo IT IS RUNNING RIGHT NOW - any install in progress will be lost.
echo.
set "ANSWER="
set /p "ANSWER=Destroy it and rebuild from scratch? [y/N] "
if /i not "%ANSWER%"=="y" (
    echo.
    echo Aborted - nothing was changed.
    echo Re-run without /force to use the VM that is already there.
    goto :done
)
goto :createvm

REM VM exists and no /force: New-AwowVm.ps1 would no-op WITHOUT starting it,
REM so handle the existing VM here instead of pretending we created one.
:existing
REM
REM First: does it have the media we were ASKED for? An existing VM keeps the
REM DVDs it was built with, so re-running with a different ISO (notably
REM /repacked) would otherwise silently boot the OLD image and look like the
REM new one failed. Recreating is the only way to change the media.
call :getdvd
call :mediacheck
if not defined MEDIA_OK (
    echo ERROR: the existing VM has different media attached than you asked for.
    echo        wanted:   %UBUNTU_ISO%
    echo        attached: %VMDVD%
    echo.
    echo        An existing VM keeps the DVDs it was created with, so booting it
    echo        would just re-run the OLD image. Re-run with  /force  to delete
    echo        this VM and its VHDX and rebuild it with the ISO above.
    goto :fail
)

if /i "%VMSTATE%"=="Running" (
    echo The VM is ALREADY RUNNING - leaving it completely alone.
    echo Not restarting it, not recreating it, not touching its disk.
    echo Re-run with  /force  to DELETE it and rebuild from a clean disk.
    echo.
    goto :connect
)
echo The VM already exists but is not running - starting it.
echo Re-run with  /force  to DELETE it and rebuild from a clean disk.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-VM -Name '%VM_NAME%'"
if errorlevel 1 (
    echo.
    echo ERROR: could not start the existing VM.
    goto :fail
)
goto :verify

:createvm
powershell -NoProfile -ExecutionPolicy Bypass -File "%VMTEST_DIR%\New-AwowVm.ps1" ^
    -UbuntuIsoPath "%UBUNTU_ISO%" ^
    -SeedIsoPath   "%SEED_ISO%" ^
    -VMPath        "%VM_PATH%" ^
    -Start%EXTRA_ARGS%
if errorlevel 1 (
    echo.
    echo ERROR: New-AwowVm.ps1 failed - see the message above.
    goto :fail
)
if defined DO_WHATIF (
    echo.
    echo /whatif - preview only, nothing was created or started.
    goto :done
)

:verify
REM Never announce success we have not confirmed.
call :getstate
if not defined VMSTATE (
    echo.
    echo ERROR: no VM named %VM_NAME% exists after the run.
    goto :fail
)
if /i not "%VMSTATE%"=="Running" (
    echo.
    echo ERROR: the VM exists but is not running - state: %VMSTATE%
    echo        Start it from Hyper-V Manager, or re-run with /force to rebuild.
    goto :fail
)

:connect
echo.
echo ======================================================================
echo   VM is RUNNING - state confirmed.
echo.
if defined DO_REPACKED goto :msg_repacked

echo   AT THE GRUB MENU - the only thing you have to type in this whole gate.
echo   The menu waits 30 seconds, so there is no rush:
echo     1. arrow DOWN to the line starting   linux  /casper/vmlinuz
echo     2. press  End  to jump to the very end of THAT line
echo     3. type a space then   autoinstall
echo     4. press  Ctrl+X  to boot
echo.
echo   DO NOT press Enter while editing. Enter splits the line, which leaves
echo   autoinstall sitting on a line of its own - GRUB then tries to run it as
echo   a command and says  "can't find command 'autoinstall'".  The word must
echo   end up ON the linux line. If you get that error, press Esc and retry,
echo   or just re-run this script with  /repacked  and type nothing at all.
echo.
echo   NO GRUB MENU? If the console is blank, the VM was probably already
echo   past it or never booted the ISO. Check with:
echo     Get-VM %VM_NAME% ^| Format-List Name,State,Uptime
echo   and re-run this script with  /force  for a clean boot from scratch.
echo.
goto :msg_common

:msg_repacked
echo   ZERO-KEYPRESS BOOT - do not touch the keyboard.
echo   The repacked ISO already carries the autoinstall args in its grub.cfg,
echo   so GRUB boots straight through and Subiquity installs unattended.
echo.

:msg_common
echo   Console login: hub
echo   Password:      %VMTEST_DIR%\.out\secrets\creds.env
echo.
echo   Then watch it come up:
echo     journalctl -u awow-firstboot -f
echo.
echo   Teardown when done:  vmtest\Remove-AwowVm.ps1
echo ======================================================================
echo.

if "%OPEN_CONSOLE%"=="1" (
    echo Opening the VM console...
    start "" vmconnect.exe localhost "%VM_NAME%"
)

:done
call :handoff
exit /b 0

REM -- how this script ends --------------------------------------------------
REM Run from an existing terminal: just exit. You are already at a shell, and
REM a "press any key" there is pure friction.
REM Self-elevated into its own window (/elevated): that window is ours and
REM closing it would throw away the output, so hand it over as a live elevated
REM prompt in this directory. Type  exit  to close it. Either way the VM is
REM started exactly once - nothing here loops back and starts it again.
:handoff
echo.
if not defined SELF_ELEVATED exit /b 0
echo ----------------------------------------------------------------------
echo   Press a key for an elevated prompt in this folder. Useful next steps:
echo     Get-VM %VM_NAME% ^| Format-List Name,State,Uptime
echo     vmconnect localhost %VM_NAME%
echo     powershell -File .\Remove-AwowVm.ps1
echo   Type  exit  to close this window. The VM keeps running either way.
echo ----------------------------------------------------------------------
pause
cd /d "%VMTEST_DIR%"
cmd /k
exit /b 0

REM -- helper: VMSTATE = current state, or undefined if no such VM -----------
:getstate
set "VMSTATE="
for /f "usebackq tokens=*" %%S in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$v = Get-VM -Name '%VM_NAME%' -ErrorAction SilentlyContinue; if ($v) { $v.State }"`) do set "VMSTATE=%%S"
exit /b 0

REM -- helper: VMDVD = semicolon-joined paths of the VM's attached DVDs -------
:getdvd
set "VMDVD="
for /f "usebackq tokens=*" %%D in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$d = Get-VMDvdDrive -VMName '%VM_NAME%' -ErrorAction SilentlyContinue; if ($d) { ($d.Path ^| Where-Object { $_ }) -join ';' }"`) do set "VMDVD=%%D"
if not defined VMDVD set "VMDVD=(none)"
exit /b 0

REM -- helper: MEDIA_OK=YES if the wanted ISO is attached to the existing VM --
REM Done in PowerShell as an exact -contains test. A batch  echo ^| find  pipe
REM looks simpler but silently mangles the quoted path when cmd builds the
REM pipe child, so it reported "no match" even for byte-identical strings.
:mediacheck
set "MEDIA_OK="
for /f "usebackq tokens=*" %%M in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$d = Get-VMDvdDrive -VMName '%VM_NAME%' -ErrorAction SilentlyContinue; if (@($d.Path) -contains '%UBUNTU_ISO%') { 'YES' }"`) do set "MEDIA_OK=%%M"
exit /b 0

:fail
call :handoff
exit /b 1
