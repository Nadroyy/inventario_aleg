# Inventario y catálogo web

Aplicación para mantener un catálogo de productos y consultar indicadores de uso a partir de accesos exitosos. Este repositorio es una versión de portafolio: no incluye credenciales, bases de datos, registros reales, imágenes de productos ni configuración de producción.

## Funcionalidades destacadas

- Catálogo web y generación de archivos Excel con productos e imágenes.
- Sincronización de inventario, precios y existencias desde servicios externos configurados mediante variables de entorno.
- Reporte de accesos con filtros por fechas y origen, métricas agregadas y listados de clientes.
- Exportación de reportes a Excel y CSV.
- Panel administrativo con autenticación y controles de transporte seguro para despliegues HTTPS.
- Pruebas automatizadas para filtros y estructura de los reportes.

## Tecnologías

Python, FastAPI, Jinja2, MySQL, openpyxl, Docker y servicios de integración configurables.

## Desarrollo local

1. Instala Python y MySQL.
2. Crea y activa un entorno virtual, e instala las dependencias:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

3. Copia `.env.example` a `.env` y configura únicamente las integraciones que usarás. No compartas ni subas ese archivo.
4. Inicializa una base local compatible con el esquema de la aplicación.
5. Inicia la aplicación desde la raíz del proyecto:

   ```powershell
   python -m uvicorn proyecto_web.app:app --host 127.0.0.1 --port 8765
   ```

   La aplicación completa requiere credenciales válidas para las integraciones habilitadas. Para desplegar el panel de reportes, configura credenciales administrativas y una cuenta MySQL de solo lectura. En producción, sirve el panel exclusivamente mediante HTTPS.

## Pruebas

```powershell
python -m unittest tests.test_access_reports -v
```

## Seguridad y datos

- Los secretos se proporcionan mediante variables de entorno; `.env` está excluido de Git.
- No uses datos personales, exportaciones de producción ni credenciales reales en pruebas o capturas.
- Los reportes contienen información identificable. Restringe su acceso y comparte las exportaciones solo con personas autorizadas.
- Los datos y activos locales están excluidos por `.gitignore`.
