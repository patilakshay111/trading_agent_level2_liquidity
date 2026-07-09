@echo off
cd /d "%~dp0"

set "PYTHON_EXE=C:\Users\akpatil\AppData\Local\Programs\Python\Python313\python.exe"

if exist "%PYTHON_EXE%" (
    "%PYTHON_EXE%" "%~dp0run_with_log.py"
    goto done
)

where python >nul 2>nul
if %ERRORLEVEL%==0 (
    python "%~dp0run_with_log.py"
    goto done
)

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3 "%~dp0run_with_log.py"
    goto done
)

echo Python was not found. Install Python 3.11+ or add Python to PATH.

:done
pause
