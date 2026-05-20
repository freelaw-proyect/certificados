#!/usr/bin/env bash
# Ejecuta el flujo HTTP completo (carro → agregar → orden → entrega → freePagado).
# Requiere REGISTROCIVIL_COOKIE o cookie.rc.txt con sesión válida del RC.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8765}"
URL="http://127.0.0.1:${PORT}/registrocivil/entrega-certificado"
echo "POST ${URL}"
curl -sS -X POST "${URL}" -H "Content-Type: application/json" -d '{}' | python3 -m json.tool
