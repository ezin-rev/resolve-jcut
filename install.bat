@echo off
REM Install AutoCut dependencies (Windows) using uv

where uv >nul 2>&1
if errorlevel 1 (
    echo uv not found -- installing via official installer
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
)

echo =^> Syncing dependencies
uv sync
if errorlevel 1 (
    echo Dependency sync failed.
    pause
    exit /b 1
)

echo.
echo Done. With DaVinci Resolve open, run:
echo   Workspace -^> Scripts -^> Utility -^> AutoCut Bridge   (free version)
echo or launch the app directly:
echo   uv run python main.py
pause
