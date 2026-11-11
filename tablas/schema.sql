-- ============================================================
-- INVENTARIO_ALEG - Esquema MySQL
-- Base de datos: inventario_local
-- ============================================================

CREATE DATABASE IF NOT EXISTS inventario_local
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE inventario_local;

-- ------------------------------------------------------------
-- Tabla principal de items
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS items (
    id               VARCHAR(20)   NOT NULL,
    nombre           VARCHAR(500)  NOT NULL,
    referencia       VARCHAR(200)  DEFAULT NULL,
    descripcion      TEXT          DEFAULT NULL,
    estado           VARCHAR(20)   NOT NULL DEFAULT 'active',   -- active | inactive
    categoria_id     INT           DEFAULT NULL,
    categoria_nombre VARCHAR(200)  DEFAULT NULL,
    tiene_iva        TINYINT(1)    NOT NULL DEFAULT 0,
    porcentaje_iva   DECIMAL(5,2)  DEFAULT NULL,
    stock_total      DECIMAL(14,4) NOT NULL DEFAULT 0,
    imagen_url       VARCHAR(1000) DEFAULT NULL,
    ultima_sync      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- Precios por lista de precios
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS item_precios (
    id               INT           NOT NULL AUTO_INCREMENT,
    item_id          VARCHAR(20)   NOT NULL,
    lista_precio_id  VARCHAR(20)   NOT NULL,
    nombre_lista     VARCHAR(200)  NOT NULL,
    precio           DECIMAL(14,2) NOT NULL DEFAULT 0,
    moneda           VARCHAR(10)   NOT NULL DEFAULT 'COP',
    es_principal     TINYINT(1)    NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uq_item_lista (item_id, lista_precio_id),
    CONSTRAINT fk_precios_item FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- Stock por bodega
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS item_bodegas (
    id               INT           NOT NULL AUTO_INCREMENT,
    item_id          VARCHAR(20)   NOT NULL,
    bodega_id        VARCHAR(20)   NOT NULL,
    bodega_nombre    VARCHAR(200)  NOT NULL,
    es_default       TINYINT(1)    NOT NULL DEFAULT 0,
    estado           VARCHAR(20)   NOT NULL DEFAULT 'active',
    stock_disponible DECIMAL(14,4) NOT NULL DEFAULT 0,
    stock_minimo     DECIMAL(14,4) NOT NULL DEFAULT 0,
    stock_maximo     DECIMAL(14,4) NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uq_item_bodega (item_id, bodega_id),
    CONSTRAINT fk_bodegas_item FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- Registro único de clientes finales (primera vez que ingresan)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS visitas_clientes (
    id           INT           NOT NULL AUTO_INCREMENT,
    nombre       VARCHAR(200)  NOT NULL,
    telefono     VARCHAR(50)   NOT NULL,
    ip           VARCHAR(45)   DEFAULT NULL,
    user_agent   VARCHAR(500)  DEFAULT NULL,
    fecha        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_telefono (telefono)   -- un registro por número
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
-- Nota: cada acceso al catálogo público queda en otp_log con modo='publico',
-- id_cliente=teléfono y correo=nombre. Misma tabla que instaladores.

-- ------------------------------------------------------------
-- Registro general de accesos a los catálogos
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS registro_accesos (
    id                 INT           NOT NULL AUTO_INCREMENT,
    nombre             VARCHAR(200)  NOT NULL,
    documento          VARCHAR(50)   DEFAULT NULL,
    email              VARCHAR(150)  DEFAULT NULL,
    telefono           VARCHAR(50)   DEFAULT NULL,
    origen             VARCHAR(20)   NOT NULL DEFAULT 'web',
    tipo_cliente       VARCHAR(40)   NOT NULL DEFAULT 'desconocido',
    alegra_registrado  TINYINT(1)    NOT NULL DEFAULT 0,
    ip                 VARCHAR(45)   DEFAULT NULL,
    user_agent         VARCHAR(500)  DEFAULT NULL,
    fecha              DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_origen      (origen),
    KEY idx_tipo_cliente (tipo_cliente),
    KEY idx_alegra      (alegra_registrado),
    KEY idx_fecha       (fecha)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- Log de sincronizaciones
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_log (
    id               INT           NOT NULL AUTO_INCREMENT,
    fecha_inicio     DATETIME      NOT NULL,
    fecha_fin        DATETIME      DEFAULT NULL,
    total_items      INT           DEFAULT 0,
    insertados       INT           DEFAULT 0,
    actualizados     INT           DEFAULT 0,
    errores          INT           DEFAULT 0,
    estado           VARCHAR(20)   NOT NULL DEFAULT 'en_progreso',  -- en_progreso | ok | error
    detalle          TEXT          DEFAULT NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
