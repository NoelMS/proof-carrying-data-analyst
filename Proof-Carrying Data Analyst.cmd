@echo off
rem Double-click to start Proof-Carrying Data Analyst. First start sets everything up.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "%~dp0launch.pyw"
  exit /b 0
)
where pyw >nul 2>nul && (
  start "" pyw -3 "%~dp0launch.pyw"
  exit /b 0
)
where pythonw >nul 2>nul && (
  start "" pythonw "%~dp0launch.pyw"
  exit /b 0
)

echo.
echo  Proof-Carrying Data Analyst needs Python 3.11 or newer, and none was found.
echo.
choice /c YN /m " Install Python 3.13 now with winget"
if errorlevel 2 (
  start "" https://www.python.org/downloads/
  exit /b 1
)
winget install -e --id Python.Python.3.13 --scope user --accept-source-agreements --accept-package-agreements
if errorlevel 1 (
  echo  Python could not be installed automatically. Get it from https://www.python.org/downloads/
  pause
  exit /b 1
)
set "PYW=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
if exist "%PYW%" (
  start "" "%PYW%" "%~dp0launch.pyw"
  exit /b 0
)
echo  Python is installed. Double-click this file again to start.
pause
