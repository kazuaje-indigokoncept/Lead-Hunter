@echo off
chcp 65001 >nul
cd /d "%~dp0"
docker info >nul 2>nul || (echo Abre Docker Desktop, espera a que diga "Engine running" y vuelve a ejecutar este archivo. & pause & exit /b)
if not exist .env (
  set /p PASS=Elige una contraseña para el usuario admin ^(mín. 8 caracteres^): 
  call :crearenv
)
if not exist data mkdir data
echo Construyendo y arrancando LeadHunter en Docker (la primera vez tarda 1-3 min)...
docker compose -f docker-compose.local.yml up -d --build || (pause & exit /b)
timeout /t 4 >nul
start "" http://localhost:8000
echo.
echo Listo: http://localhost:8000   usuario: admin
echo Para apagarlo:  docker compose -f docker-compose.local.yml down
pause
exit /b

:crearenv
(
  echo ADMIN_USER=admin
  echo ADMIN_PASSWORD=%PASS%
  echo SITE_ADDRESS=:80
  echo SECURE_COOKIE=0
) > .env
exit /b
