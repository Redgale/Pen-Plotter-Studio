@echo off
REM Console build for debugging -- shows real Python errors instead of a
REM silent crash. Run the resulting exe from Command Prompt, not by
REM double-clicking, so the window stays open after it exits.
setlocal
set SCRIPT_DIR=%~dp0
set REPO_ROOT=%SCRIPT_DIR%..\..

echo ============================================
echo   PenPlotter Studio - DEBUG build V1.4
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    pause
    exit /b 1
)

python -m pip install --upgrade pip
python -m pip install -r "%SCRIPT_DIR%requirements.txt"

echo.
echo Building PenPlotterStudio-debug.exe...
python -m PyInstaller --noconfirm --onefile --console ^
    --distpath "%SCRIPT_DIR%dist" ^
    --workpath "%SCRIPT_DIR%build" ^
    --specpath "%SCRIPT_DIR%" ^
    --name PenPlotterStudio-debug ^
    --icon "%REPO_ROOT%\resources\icon.ico" ^
    --add-data "%REPO_ROOT%\resources;resources" ^
    "%REPO_ROOT%\src\main.py"

echo.
echo ============================================
echo   Done: %SCRIPT_DIR%dist\PenPlotterStudio-debug.exe
echo   Run it from a Command Prompt window so any
echo   crash's real error text stays visible.
echo ============================================
pause
