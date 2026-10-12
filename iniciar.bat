@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0iniciar.ps1" %*
set "LH_RESULT=%ERRORLEVEL%"
if not "%LH_RESULT%"=="0" (
  echo.
  echo LeadHunter no ha podido arrancar. Revisa el error de arriba.
  echo Puedes ejecutar diagnostico.bat para comprobar la instalacion.
)
pause
exit /b %LH_RESULT%
