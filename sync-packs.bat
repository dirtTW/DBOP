@echo off
setlocal
set "SCRIPT=%~dp0sync-packs.py"

where python >nul 2>nul
if errorlevel 1 goto try_py
python "%SCRIPT%" %*
goto done

:try_py
where py >nul 2>nul
if errorlevel 1 goto no_python
py -3 "%SCRIPT%" %*
goto done

:no_python
echo Python 3.11 or newer is required. Install it and run this file again.
exit /b 1

:done
if "%~1"=="" pause
exit /b %ERRORLEVEL%
