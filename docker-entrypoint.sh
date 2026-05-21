#!/bin/sh
set -e

# Pantalla virtual para Playwright "headed" en Linux (Render/Docker sin monitor).
# Útil si REGISTROCIVIL_WS_BROWSER_HEADLESS=false (reCAPTCHA a veces exige no-headless técnico).
USE_XVFB="${REGISTROCIVIL_USE_XVFB:-}"
HEADLESS="${REGISTROCIVIL_WS_BROWSER_HEADLESS:-true}"

case "$(echo "$USE_XVFB" | tr '[:upper:]' '[:lower:]')" in
  true|1|yes) START_XVFB=1 ;;
  *) START_XVFB=0 ;;
esac

case "$(echo "$HEADLESS" | tr '[:upper:]' '[:lower:]')" in
  false|0|no) START_XVFB=1 ;;
esac

if [ "$START_XVFB" = "1" ] && [ -z "${DISPLAY:-}" ]; then
  echo "[entrypoint] Iniciando Xvfb en :99 (pantalla virtual para Chromium)…"
  Xvfb :99 -screen 0 1280x900x24 -ac +extension GLX +render -noreset &
  export DISPLAY=:99
  sleep 1
fi

exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8765}"
