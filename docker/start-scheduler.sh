#!/bin/sh
# ─────────────────────────────────────────────────────────────
#  Arranque del contenedor scheduler (cron del catálogo y la hoja de bodegas).
#
#  Problema clásico: cron NO hereda las variables de entorno del
#  contenedor (las que pasa Portainer/compose vía env_file). Por eso
#  volcamos el entorno actual a un archivo y lo cargamos en cada job.
# ─────────────────────────────────────────────────────────────
set -e

echo "[scheduler] Preparando entorno para cron..."

# 1. Volcar variables de entorno actuales a un archivo que cron pueda leer.
#    (se excluyen las que no son exportables limpiamente)
printenv | grep -vE '^(PWD|SHLVL|_|HOME|TERM)=' | sed 's/^/export /' > /app/docker/env.sh
chmod 600 /app/docker/env.sh

# 2. Instalar el crontab, inyectando el 'source' del entorno antes de cada job.
#    Reescribimos cada línea de comando para que primero cargue env.sh.
{
  echo "# Generado por start-scheduler.sh — no editar a mano"
  while IFS= read -r line; do
    case "$line" in
      ''|\#*) echo "$line" ;;                      # comentarios y líneas en blanco tal cual
      *) echo "$line" | sed 's#cd /app#. /app/docker/env.sh; cd /app#' ;;
    esac
  done < /app/docker/crontab
} > /etc/cron.d/catalogo

chmod 0644 /etc/cron.d/catalogo
crontab /etc/cron.d/catalogo

# 3. Asegurar que exista el log, rotar si supera 10MB, y arrancar cron.
touch /app/data/cron_catalogo.log

# Rotación simple: si el log supera 10MB, renombrar y crear uno nuevo
LOG_FILE=/app/data/cron_catalogo.log
MAX_BYTES=10485760  # 10MB
if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt "$MAX_BYTES" ]; then
    mv "$LOG_FILE" "${LOG_FILE%.log}_$(date '+%Y%m%d').log"
    touch "$LOG_FILE"
    echo "[scheduler] Log rotado (superó 10MB)."
    # Conservar solo los últimos 3 logs rotados
    ls -t /app/data/cron_catalogo_*.log 2>/dev/null | tail -n +4 | xargs rm -f 2>/dev/null || true
fi
echo "[scheduler] Catálogo: 03:00, 10:00, 13:00, 16:00 y 17:00; hoja de bodegas: 08:00, 15:00 y 18:00 (America/Bogota)."
echo "[scheduler] Siguiendo el log de actualizaciones..."

# cron en foreground (-f) para que el contenedor no muera; seguimos el log.
cron -f &
tail -F /app/data/cron_catalogo.log
