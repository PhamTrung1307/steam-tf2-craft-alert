@echo off
setlocal
cd /d "%~dp0"
python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
if exist icon.ico (
    python -m PyInstaller --onefile --windowed --name TF2CraftAlert --icon icon.ico app.py
) else (
    python -m PyInstaller --onefile --windowed --name TF2CraftAlert app.py
)
if errorlevel 1 exit /b 1
endlocal
