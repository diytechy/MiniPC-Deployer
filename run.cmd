@echo off
setlocal
REM Product launcher (Windows) — double-click to run this project.
REM Every launchable project ships run.cmd / run.sh / run.command (process.md
REM section 7, "the evaluator's rungs") so starting it never requires recalling
REM a command. Read it first; it only runs the one command below.
REM
REM Not applicable (a pure library)? Delete the run.* launchers and describe
REM usage in README.md instead.

REM --- EDIT FOR YOUR PROJECT ---------------------------------------------------
REM The command that starts the product, run from the repo root. Examples:
REM   set "RUN_CMD=python -m yourapp"
REM   set "RUN_CMD=go run ./cmd/yourapp serve"
REM   set "RUN_CMD=npm start"
REM Keep run.sh's RUN_CMD in sync — it is the POSIX twin; run.command delegates
REM to it, so the command lives exactly twice: here and there.
set "RUN_CMD="
REM ----------------------------------------------------------------------------

cd /d "%~dp0"
if not defined RUN_CMD (
  echo run.cmd: no launch command wired yet.
  echo Edit RUN_CMD in this file — see the EDIT FOR YOUR PROJECT block — and
  echo in run.sh. The README "Run it" section documents the underlying command.
  pause
  exit /b 1
)
echo Running: %RUN_CMD% %*
%RUN_CMD% %*
set "EXITCODE=%ERRORLEVEL%"
echo.
echo Exited with code %EXITCODE%.
pause
exit /b %EXITCODE%
