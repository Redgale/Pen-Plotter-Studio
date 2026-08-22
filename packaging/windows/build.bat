@echo off
REM Run this from anywhere -- it locates the repo root relative to itself.
setlocal
set SCRIPT_DIR=%~dp0
set REPO_ROOT=%SCRIPT_DIR%..\..

echo ============================================
echo   PenPlotter Studio - Windows build
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    echo Install Python 3.11+ from https://www.python.org/downloads/
    echo and check "Add python.exe to PATH" during install.
    pause
    exit /b 1
)

echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r "%SCRIPT_DIR%requirements.txt"
if errorlevel 1 (
    echo ERROR: dependency install failed, see above.
    pause
    exit /b 1
)

echo.
echo Building PenPlotterStudio.exe...
python -m PyInstaller --noconfirm --onefile --windowed ^
    --distpath "%SCRIPT_DIR%dist" ^
    --workpath "%SCRIPT_DIR%build" ^
    --specpath "%SCRIPT_DIR%" ^
    --name PenPlotterStudio ^
    --icon "%REPO_ROOT%\resources\icon.ico" ^
    --add-data "%REPO_ROOT%\resources;resources" ^
    "%REPO_ROOT%\src\main.py"

if errorlevel 1 (
    echo ERROR: build failed, see above.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Done! Your exe is at: %SCRIPT_DIR%dist\PenPlotterStudio.exe
echo ============================================
pause
