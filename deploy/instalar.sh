#!/usr/bin/env bash
# Instalador de LeadHunter para un VPS Ubuntu/Debian (Hostinger KVM, etc.)
# Uso:  sudo bash deploy/instalar.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "$(id -u)" -ne 0 ]; then echo "Ejecuta con sudo o como root."; exit 1; fi

echo "==> 1/4 Instalando Docker (si hace falta)…"
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null

echo "==> 2/4 Configurando .env…"
if [ ! -f .env ]; then
  IP=$(curl -fsS https://api.ipify.org || hostname -I | awk '{print $1}')
  PASS=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 14)
  cat > .env <<EOT
ADMIN_USER=admin
ADMIN_PASSWORD=$PASS
SITE_ADDRESS=${IP//./-}.sslip.io
SECURE_COOKIE=1
EOT
  chmod 600 .env
  echo "    Dirección: https://${IP//./-}.sslip.io"
  echo "    Usuario: admin   Contraseña: $PASS   (anótala y cámbiala al entrar)"
fi

echo "==> 3/4 Abriendo puertos 22, 80 y 443 en el firewall…"
if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null
fi

echo "==> 4/4 Construyendo y arrancando…"
mkdir -p data && chown 1000:1000 data 2>/dev/null || true
docker compose up -d --build
sleep 3
docker compose ps
grep SITE_ADDRESS .env
echo "Listo. Abre la dirección de arriba con https:// (el certificado tarda ~1 min la primera vez)."
