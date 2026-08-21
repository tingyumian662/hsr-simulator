@echo off
chcp 65001 >nul
cd /d "%~dp0模拟器本体"
echo ==========================================
echo   Star Rail Simulator - starting...
echo ==========================================

REM  Check Python (try "python" first, fall back to Windows "py" launcher)
set PYTHON_CMD=python
python --version >nul 2>&1
if errorlevel 1 (
    py -3 --version >nul 2>&1
    if errorlevel 1 (
        echo.
        echo   [ERROR] Python not found.
        echo   Please install Python 3.10 or newer first:
        echo     https://www.python.org/downloads/
        echo.
        echo   IMPORTANT: during installation, check the box
        echo   "Add Python to PATH", then reopen this file.
        echo.
        pause
        exit /b 1
    )
    set PYTHON_CMD=py -3
)

REM  Check dependencies (auto-install on first run)
%PYTHON_CMD% -c "import fastapi, uvicorn, pydantic, jinja2" >nul 2>&1
if errorlevel 1 (
    echo First run: installing dependencies, please wait...
    %PYTHON_CMD% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo   [ERROR] Failed to install dependencies.
        echo   Check your network connection, then run this file again.
        echo.
        pause
        exit /b 1
    )
    echo Dependencies installed.
)

REM  Start server, open browser shortly after
echo Starting http://127.0.0.1:8000 ...
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:8000"
REM Use main.py entry point (script dir goes to sys.path, stable on any Python version)
%PYTHON_CMD% main.py

pause
