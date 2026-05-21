# Imagen oficial: Chromium + dependencias ya instalados (debe coincidir con playwright en requirements).
# https://playwright.dev/docs/docker
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    REGISTROCIVIL_BUG_CLEAR_ON_START=false \
    REGISTROCIVIL_BROWSER_BACKEND=playwright

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY rc_*.py ./
COPY entregar.sh start.sh cookie.rc.txt.example ./

# Carpetas vacías para escritura en runtime (Render disco efímero)
RUN mkdir -p incoming salida bug && chmod +x entregar.sh start.sh 2>/dev/null || true

EXPOSE 10000

# Render inyecta PORT; local usa 8765 por defecto
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8765}"]
