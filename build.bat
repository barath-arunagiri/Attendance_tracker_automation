@echo off
REM Builds AttendanceAutomation.exe. The window STAYS OPEN at the end so you can read the result.
setlocal
cd /d %~dp0
echo Building in: %cd%
if not exist app.py (
  echo ERROR: app.py not found here. Run this file from the AttendanceAutomation folder.
  goto :err
)

echo Stopping any running copy ^(a running copy locks files and breaks the build^)...
taskkill /F /IM AttendanceAutomation.exe >nul 2>&1
taskkill /F /IM AttendanceAgent.exe >nul 2>&1
powershell -NoProfile -Command "Get-Process node -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*AttendanceAutomation*' -or $_.Path -like '*AttendanceAgent*' } | Stop-Process -Force" >nul 2>&1
timeout /t 2 /nobreak >nul
if exist dist\AttendanceAutomation rmdir /S /Q dist\AttendanceAutomation
if exist dist\AttendanceAutomation (
  echo ERROR: dist\AttendanceAutomation is still locked. Close it in Task Manager ^(AttendanceAutomation / Node.js^), or restart the laptop, then run again.
  goto :err
)

set PY=py -3.12
%PY% --version >nul 2>&1 || set PY=py -3
%PY% -m pip install -r requirements.txt pyinstaller || goto :err

if not exist node\node.exe (
  powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest https://nodejs.org/dist/v22.12.0/node-v22.12.0-win-x64.zip -OutFile node.zip; Expand-Archive node.zip -DestinationPath nodetmp -Force; Move-Item nodetmp\node-v22.12.0-win-x64 node -Force; Remove-Item node.zip,nodetmp -Recurse -Force" || goto :err
)
pushd wa
call ..\node\npm.cmd install --omit=dev || (popd & goto :err)
popd

%PY% -m PyInstaller --noconfirm --clean --noconsole --name AttendanceAutomation app.py || goto :err
xcopy /E /I /Y wa dist\AttendanceAutomation\wa >nul
xcopy /E /I /Y node dist\AttendanceAutomation\node >nul
echo.
echo DONE. New app is in: %cd%\dist\AttendanceAutomation\AttendanceAutomation.exe
echo Your old settings and WhatsApp link are copied over automatically on first start.
pause
exit /b 0
:err
echo.
echo BUILD FAILED - read the messages above.
pause
exit /b 1
