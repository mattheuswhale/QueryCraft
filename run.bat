@echo off
setlocal
cd /d "%~dp0"

set "VENV_DIR=.venv"
set "SETUP_MARKER=%VENV_DIR%\.querycraft_setup_complete"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"

if not exist "%VENV_DIR%" (
    echo [setup] Creating virtual environment...
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3.11 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" >nul 2>nul
        if not errorlevel 1 (
            py -3.11 -m venv "%VENV_DIR%"
            goto :venv_created
        )
    )

    where python >nul 2>nul
    if errorlevel 1 goto :python_missing
    python -c "import sys; raise SystemExit(0 if (3, 9) <= sys.version_info[:2] < (3, 12) else 1)" >nul 2>nul
    if errorlevel 1 goto :python_wrong_version
    python -m venv "%VENV_DIR%"
)

:venv_created
if not exist "%PYTHON_EXE%" (
    echo [error] Virtual environment is incomplete: "%PYTHON_EXE%" was not found.
    exit /b 1
)

if not exist "%SETUP_MARKER%" (
    echo [setup] Installing Python packages...
    "%PYTHON_EXE%" -m pip install --upgrade pip setuptools wheel || goto :setup_failed
    "%PYTHON_EXE%" -m pip install -r requirements.txt || goto :setup_failed
    "%PYTHON_EXE%" -m pip install -r requirements_mcp.txt || goto :setup_failed
    "%PYTHON_EXE%" -m pip install -r requirements_korean.txt || goto :setup_failed

    echo [setup] Downloading UniDic dictionary...
    "%PYTHON_EXE%" -m unidic download || goto :setup_failed

    type nul > "%SETUP_MARKER%"
    echo [setup] First-run setup complete.
)

echo [run] Starting QueryCraft...
"%PYTHON_EXE%" assistant.py
set "EXIT_CODE=%ERRORLEVEL%"
exit /b %EXIT_CODE%

:python_missing
echo [error] Python was not found. Install Python 3.11, then run this file again.
exit /b 1

:python_wrong_version
echo [error] MeloTTS currently needs Python 3.9-3.11 for this project.
echo [error] Install Python 3.11 from python.org, then run this file again.
exit /b 1

:setup_failed
echo [error] First-run setup failed. Fix the error above and run this file again.
exit /b 1
