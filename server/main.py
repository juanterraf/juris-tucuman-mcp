"""Servidor MCP de jurisprudencia del Poder Judicial de Tucumán.

Expone tres tools de consulta contra el portal público
juris.justucuman.gov.ar:

  - buscar_fallos: búsqueda multi-campo
  - descargar_texto_fallo: texto íntegro de un fallo
  - listar_fallos_recientes: últimos fallos publicados

Sin estado en disco. Cada llamada consulta el portal en vivo (necesita
que la IP saliente del proceso pueda acceder a juris.justucuman.gov.ar
— típicamente cualquier IP residencial argentina).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Annotated

# Asegurar que `scraper` sea importable cuando se arranca como subprocess
# por Claude Desktop (cwd impredecible).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# También agregar lib/ para deps bundleadas (MCPB)
_LIB = os.path.join(_HERE, "lib")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from mcp.server.fastmcp import FastMCP  # noqa: E402
from pydantic import Field  # noqa: E402

import scraper  # noqa: E402

log = logging.getLogger("juris-tucuman-mcp")

mcp = FastMCP(
    name="juris-tucuman",
    instructions=(
        "Servidor MCP de jurisprudencia del Poder Judicial de Tucumán "
        "(Argentina). Consulta fallos publicados en "
        "juris.justucuman.gov.ar. Cada llamada va en vivo al portal. "
        "La información NO constituye asesoramiento jurídico — verificá "
        "siempre contra la fuente oficial."
    ),
)

# Sesión HTTP reutilizable entre llamadas (mantiene cookies/session_vistab).
_sesion: scraper.requests.Session | None = None


def _get_sesion() -> scraper.requests.Session:
    global _sesion
    if _sesion is None:
        log.info("Creando sesión HTTP nueva")
        _sesion = scraper.crear_sesion()
    return _sesion


def _reset_sesion() -> scraper.requests.Session:
    global _sesion
    log.warning("Reseteando sesión HTTP")
    _sesion = scraper.crear_sesion()
    return _sesion


# ===================================================================
# Tools
# ===================================================================

@mcp.tool(
    name="buscar_fallos",
    description=(
        "Busca fallos en el portal de jurisprudencia del PJ Tucumán. "
        "Acepta combinaciones de filtros: descriptores (palabras clave), "
        "carátula, partes, número de sentencia o expediente, rango de "
        "fechas y tribunal. Devuelve metadatos + sumario de cada fallo."
    ),
)
def buscar_fallos(
    descriptores: Annotated[str, Field(
        default="",
        description="Palabras clave temáticas (ej: 'despido discriminatorio', 'prescripción adquisitiva')."
    )] = "",
    caratula: Annotated[str, Field(
        default="",
        description="Texto que aparece en la carátula del expediente."
    )] = "",
    actor: Annotated[str, Field(
        default="",
        description="Nombre de la parte actora."
    )] = "",
    demandado: Annotated[str, Field(
        default="",
        description="Nombre de la parte demandada."
    )] = "",
    sentencia: Annotated[str, Field(
        default="",
        description="Número de sentencia (ej: '803')."
    )] = "",
    expte: Annotated[str, Field(
        default="",
        description="Número de expediente (ej: '487/19')."
    )] = "",
    fecha_desde: Annotated[str, Field(
        default="",
        description="Fecha mínima en formato dd/mm/aaaa."
    )] = "",
    fecha_hasta: Annotated[str, Field(
        default="",
        description="Fecha máxima en formato dd/mm/aaaa."
    )] = "",
    tribunal: Annotated[str, Field(
        default="",
        description=(
            "Nombre del tribunal o cámara (ej: 'CAMARA DEL TRABAJO', "
            "'Tribunal de Impugnación Capital', 'Cámara Penal'). Acepta "
            "cualquier capitalización, tildes y matching parcial. IMPORTANTE: "
            "el portal trata este filtro como TEMÁTICO/MATERIA, no como "
            "origen estricto — ej. filtrar por 'CAMARA DEL TRABAJO' también "
            "trae fallos laborales de la CSJ. Si hay ambigüedad, la tool "
            "devuelve los candidatos. Usá `listar_tribunales` si no estás "
            "seguro del nombre exacto."
        ),
    )] = "",
    max_paginas: Annotated[int, Field(
        default=3, ge=1, le=10,
        description="Máximo de páginas a recorrer (50 fallos por página)."
    )] = 3,
) -> dict:
    """Búsqueda en el portal."""
    filtros_no_vacios = any([
        descriptores, caratula, actor, demandado, sentencia, expte,
        fecha_desde, fecha_hasta, tribunal,
    ])
    if not filtros_no_vacios:
        return {
            "error": "Especificá al menos un filtro.",
            "cantidad": 0, "fallos": [],
        }

    # Resolver el nombre del tribunal a su código del portal.
    tribunal_code = ""
    if tribunal:
        code, candidatos = scraper.resolver_tribunal(tribunal)
        if code is None:
            if candidatos:
                return {
                    "error": (
                        f"Ambigüedad en tribunal='{tribunal}'. "
                        "Sé más específico. Candidatos:"
                    ),
                    "candidatos": candidatos,
                    "cantidad": 0, "fallos": [],
                }
            return {
                "error": (
                    f"No reconozco el tribunal '{tribunal}'. "
                    "Llamá a `listar_tribunales` para ver la lista válida."
                ),
                "cantidad": 0, "fallos": [],
            }
        tribunal_code = code

    sesion = _get_sesion()
    try:
        fallos = scraper.buscar(
            sesion,
            descriptores=descriptores, causa=caratula,
            actor=actor, demandado=demandado,
            sentencia=sentencia, nexpte=expte,
            fechad=fecha_desde, fechah=fecha_hasta,
            tribunalo=tribunal_code, max_paginas=max_paginas,
        )
    except Exception as e:
        log.exception("Búsqueda falló, reintento con sesión nueva")
        sesion = _reset_sesion()
        try:
            fallos = scraper.buscar(
                sesion,
                descriptores=descriptores, causa=caratula,
                actor=actor, demandado=demandado,
                sentencia=sentencia, nexpte=expte,
                fechad=fecha_desde, fechah=fecha_hasta,
                tribunalo=tribunal_code, max_paginas=max_paginas,
            )
        except Exception as e2:
            return {
                "error": (
                    f"No pude consultar el portal: {e2}. "
                    "Verificá tu conexión a internet o si el portal "
                    "bloquea tu IP."
                ),
                "cantidad": 0, "fallos": [],
            }

    return {
        "cantidad": len(fallos),
        "tribunal_resuelto": (
            scraper.TRIBUNALES[tribunal_code] if tribunal_code else None
        ),
        "fallos": [f.as_dict() for f in fallos],
    }


@mcp.tool(
    name="descargar_texto_fallo",
    description=(
        "Descarga el TEXTO COMPLETO de un fallo (no solo el sumario). "
        "Útil para citar in extenso o analizar argumentos. Requiere haber "
        "obtenido el número de registro vía `buscar_fallos` o "
        "`listar_fallos_recientes`."
    ),
)
def descargar_texto_fallo(
    registro: Annotated[str, Field(
        description="Número de registro del fallo (ej: '00077887' o '00077887-01')."
    )],
) -> dict:
    sesion = _get_sesion()
    try:
        texto = scraper.descargar_texto_fallo(sesion, registro)
    except Exception:
        log.exception("Error descargando fallo %s, reintento con sesión nueva", registro)
        sesion = _reset_sesion()
        try:
            texto = scraper.descargar_texto_fallo(sesion, registro)
        except Exception as e:
            return {"error": f"Error al descargar el fallo: {e}"}

    if not texto:
        return {
            "error": (
                "No pude obtener el texto del fallo. Puede que el "
                "registro sea incorrecto o que el portal no responda."
            ),
        }

    return {
        "registro": registro,
        "longitud_caracteres": len(texto),
        "texto": texto,
    }


@mcp.tool(
    name="listar_fallos_recientes",
    description=(
        "Atajo para listar los fallos más recientes en el portal, "
        "opcionalmente filtrando por tribunal. Útil para ver 'qué hay "
        "nuevo' en un fuero o cámara específica."
    ),
)
def listar_fallos_recientes(
    tribunal: Annotated[str, Field(
        default="",
        description=(
            "Nombre del tribunal (opcional, acepta cualquier capitalización "
            "y matching parcial). Ej: 'Tribunal de Impugnación', 'Cámara "
            "Penal'. Vacío = todos los tribunales."
        ),
    )] = "",
    limit: Annotated[int, Field(
        default=20, ge=1, le=100,
        description="Cuántos fallos devolver (max 100)."
    )] = 20,
) -> dict:
    # Resolver tribunal a código si vino
    tribunal_code = ""
    if tribunal:
        code, candidatos = scraper.resolver_tribunal(tribunal)
        if code is None:
            if candidatos:
                return {
                    "error": (
                        f"Ambigüedad en tribunal='{tribunal}'. "
                        "Sé más específico."
                    ),
                    "candidatos": candidatos,
                    "cantidad": 0, "fallos": [],
                }
            return {
                "error": (
                    f"No reconozco el tribunal '{tribunal}'. "
                    "Llamá a `listar_tribunales` para ver la lista válida."
                ),
                "cantidad": 0, "fallos": [],
            }
        tribunal_code = code

    sesion = _get_sesion()
    # El portal exige al menos UN filtro. Si no hay tribunal, mandamos
    # descriptores=' ' como wildcard.
    try:
        fallos = scraper.buscar(
            sesion,
            descriptores=" " if not tribunal_code else "",
            tribunalo=tribunal_code,
            max_paginas=max(1, (limit + 49) // 50),
            cantsuma=min(50, limit),
        )
    except Exception as e:
        log.exception("Listado reciente falló")
        return {
            "error": f"No pude consultar el portal: {e}",
            "cantidad": 0, "fallos": [],
        }

    fallos = fallos[:limit]
    return {
        "cantidad": len(fallos),
        "tribunal_resuelto": (
            scraper.TRIBUNALES[tribunal_code] if tribunal_code else None
        ),
        "fallos": [f.as_dict() for f in fallos],
    }


@mcp.tool(
    name="listar_tribunales",
    description=(
        "Devuelve la lista canónica de tribunales soportados por el "
        "portal con sus códigos. Útil cuando el usuario menciona un "
        "tribunal con un nombre ambiguo y querés ver las opciones."
    ),
)
def listar_tribunales() -> dict:
    return {
        "cantidad": len(scraper.TRIBUNALES),
        "tribunales": [
            {"codigo": c, "nombre": n}
            for c, n in scraper.TRIBUNALES.items()
        ],
    }


# ===================================================================
# Entrypoint
# ===================================================================

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Juris Tucumán MCP Server")
    parser.add_argument(
        "--transport", choices=["stdio", "streamable-http"],
        default="stdio",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,  # stdio MCP usa stdout, no contaminamos
    )

    if args.transport == "stdio":
        log.info("Arrancando MCP en stdio")
        mcp.run(transport="stdio")
    else:
        log.info("Arrancando MCP en %s:%s", args.host, args.port)
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
