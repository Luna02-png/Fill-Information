@echo off
REM ==== CAI DAT YUPOO AI RUNNER (chay 1 lan) ====
REM Dung Python trong D:\Projects\yupoo_tool\.venv
setlocal
set "DIR=%~dp0"
set "SCRIPT=%DIR%yupoo_ai_runner.py"
set "VENV=D:\Projects\yupoo_tool\.venv"
set "PY=%VENV%\Scripts\python.exe"
set "PYW=%VENV%\Scripts\pythonw.exe"

if not exist "%PY%" (
  echo Khong thay %PY%
  echo Sua dong "set VENV=..." trong file nay cho dung thu muc .venv roi chay lai.
  pause
  exit /b 1
)

echo [1/4] Cai thu vien vao .venv...
"%PY%" -m pip install --upgrade playwright requests gspread google-auth ddgs
"%PY%" -m playwright install chromium

echo [2/4] Dang ky link yupooai:// (nut "Mo Python" tren Google Sheet)...
reg add "HKCU\Software\Classes\yupooai" /ve /d "URL:Yupoo AI Runner" /f >nul
reg add "HKCU\Software\Classes\yupooai" /v "URL Protocol" /d "" /f >nul
reg add "HKCU\Software\Classes\yupooai\shell\open\command" /ve /d "\"%PYW%\" \"%SCRIPT%\"" /f >nul

echo [3/4] Cho Python tu bat cung Windows...
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
> "%STARTUP%\yupoo_ai_runner.bat" echo @start "" "%PYW%" "%SCRIPT%"

echo [4/4] Bat Python chay ngam...
start "" "%PYW%" "%SCRIPT%"
echo.
echo XONG! Python dang chay ngam. Nhat ky: %DIR%yupoo_ai.log
echo Muon tat tu bat: xoa file "%STARTUP%\yupoo_ai_runner.bat"
pause
