# ─────────────────────────────────────────────────────────────
#  Catálogo Alianxa — imagen de producción (FastAPI + tareas)
#  La misma imagen sirve para el servicio web y para el scheduler;
#  cambia solo el comando en docker-compose.
# ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Salida sin buffer (logs en tiempo real) y sin .pyc
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RUNNING_IN_DOCKER=1 \
    TZ=America/Bogota

# Zona horaria correcta (crítica: los horarios de actualización
# dependen de que el contenedor esté en GMT-5) + cron para el scheduler
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata cron \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instalar dependencias primero (aprovecha la cache de capas)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código de la app (data/ y .env se montan como volúmenes)
COPY src/ ./src/
COPY proyecto_web/ ./proyecto_web/
COPY proyecto_ecxel/ ./proyecto_ecxel/
COPY excel_ai/ ./excel_ai/
COPY actualizar_catalogo.py .
COPY docker/ ./docker/

EXPOSE 5000

# Por defecto arranca la web. El scheduler sobrescribe este comando.
CMD ["uvicorn", "proyecto_web.app:app", "--host", "0.0.0.0", "--port", "5000"]
