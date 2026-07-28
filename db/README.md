# `db/` — Configuraciones de despliegue

Cada archivo `.ini` aquí es una configuración completa e independiente del sistema: un cliente, un canal, un sitio. Sólo **una** está activa a la vez.

## Formato del nombre

```
db/<número>_<nombre-libre>.ini
```

- `<número>` define el orden en el desplegable (1, 2, 10, …).
- `<nombre-libre>` lo eliges tú (ej.: `telemedellin`, `canal_medellin`, …).
- Siempre terminan en `.ini`.

Ejemplos:

```
db/1_telemedellin.ini
db/2_canal_local.ini
db/3_pruebas.ini
```

## Selección del archivo activo

Por prioridad:

1. Variable de entorno **`ASTRA_INI`**, ej.: `ASTRA_INI=db/2_canal_local.ini python app.py`.
2. **`config.json > active_config`**, ej.: `{"active_config": "db/3_pruebas.ini"}` (lo modifica la UI al cambiar).
3. Primer archivo `*.ini` alfabético en `db/`.
4. Legado: `cadena_rcn.ini` en la raíz del repo (compatibilidad con despliegues viejos).

## Cómo crear un despliegue nuevo

- **Desde la UI**: botón `Nuevo INI` (junto al 📝 INI) → escribe el nombre → opcionalmente pegar contenido → guardar. Se activa con un click en el desplegable.
- **Por shell**: crea el archivo en `db/` con un editor, luego actívalo desde la UI o vía `config.json`.
- **En un equipo nuevo**: `python app.py --init` migra el `cadena_rcn.ini` de la raíz o crea uno desde `cadena_rcn.ini.example`.

## Lo que NO se commitea

`db/*.ini` y `db/*.ini.bak` están en `.gitignore`. Cada despliegue tiene sus propios datos. Sí se commitea `cadena_rcn.ini.example` (plantilla sanitizada en la raíz).
