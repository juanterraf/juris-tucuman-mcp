"""Servidor MCP de jurisprudencia del Poder Judicial de Tucumán — v1.0.

Expone 5 tools de consulta contra el portal público juris.justucuman.gov.ar:

  - buscar_fallos: búsqueda multi-campo con validación
  - obtener_fallo_por_registro: trae UN fallo (metadata + texto) en 1 llamada
  - descargar_texto_fallo: solo el texto íntegro (compat con v0.1.x)
  - listar_fallos_recientes: últimos fallos publicados (por tribunal opcional)
  - listar_tribunales: catálogo canónico (79 tribunales con sub-salas)
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Annotated

# Asegurar que `scraper`/`validators` sean importables
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_LIB = os.path.join(_HERE, "lib")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from mcp.server.fastmcp import FastMCP  # noqa: E402
from pydantic import Field  # noqa: E402

import scraper  # noqa: E402
import validators  # noqa: E402

log = logging.getLogger("juris-tucuman-mcp")

mcp = FastMCP(
    name="juris-tucuman",
    instructions=(
        "Servidor MCP de jurisprudencia del Poder Judicial de Tucumán "
        "(Argentina). Consulta fallos publicados en "
        "juris.justucuman.gov.ar. Cada llamada va en vivo al portal. "
        "La información NO constituye asesoramiento jurídico — verificá "
        "siempre contra la fuente oficial. Caveats clave del portal: "
        "(1) tokens de búsqueda ≤3 chars se ignoran (devuelven catálogo "
        "completo); usá términos de ≥4 caracteres. "
        "(2) AND implícito entre palabras; comillas para frase exacta; "
        "no soporta OR. "
        "(3) Tildes/ñ se ignoran en el portal (busca prescripción "
        "matchea prescripcion). "
        "(4) Para filtros temporales, AMBAS fecha_desde y fecha_hasta "
        "deben estar presentes; si pasás solo una, el server "
        "autocompleta la otra para que el filtro funcione."
    ),
)


# ===================================================================
# Sesión HTTP compartida (lock + TTL para reciclar PHPSESSID expirados)
# ===================================================================

_SESION_TTL_SEG = 600  # 10 minutos
_MAX_CHARS_TEXTO_FALLO = 60_000

_sesion: scraper.requests.Session | None = None
_sesion_creada_en: float = 0.0
_sesion_lock = threading.Lock()


def _get_sesion() -> scraper.requests.Session:
    global _sesion, _sesion_creada_en
    with _sesion_lock:
        ahora = time.time()
        if _sesion is None or (ahora - _sesion_creada_en) > _SESION_TTL_SEG:
            log.info("Creando sesión HTTP nueva (TTL %ds expirado o primer uso)",
                     _SESION_TTL_SEG)
            _sesion = scraper.crear_sesion()
            _sesion_creada_en = ahora
        return _sesion


def _reset_sesion() -> scraper.requests.Session:
    global _sesion, _sesion_creada_en
    with _sesion_lock:
        log.warning("Reseteando sesión HTTP")
        _sesion = scraper.crear_sesion()
        _sesion_creada_en = time.time()
        return _sesion


# ===================================================================
# Helpers comunes
# ===================================================================

def _formatear_fallo_para_response(f: scraper.Fallo, *, incluir_texto: bool = False) -> dict:
    """Devuelve un dict listo para el response del MCP."""
    d = f.as_dict()
    if not incluir_texto:
        d.pop("texto_completo", None)
    return d


def _resolver_tribunal_o_error(
    texto: str, *, tabla: dict[str, str], nombre_tabla: str,
) -> tuple[str | None, dict | None]:
    """Resuelve tribunal o retorna error estructurado."""
    code, candidatos = scraper.resolver_tribunal(texto, tabla=tabla)
    if code is None:
        if candidatos:
            return None, {
                "error": (
                    f"Ambigüedad en tribunal='{texto}'. "
                    f"Sé más específico. Candidatos en {nombre_tabla}:"
                ),
                "candidatos": candidatos,
                "cantidad": 0, "fallos": [],
            }
        return None, {
            "error": (
                f"No reconozco el tribunal '{texto}'. "
                f"Llamá a `listar_tribunales` para ver la lista válida."
            ),
            "cantidad": 0, "fallos": [],
        }
    return code, None


def _ejecutar_busqueda(**kwargs) -> scraper.ResultadoBusqueda:
    """Wrapper que reintenta con sesión nueva si la primera tirada falla."""
    sesion = _get_sesion()
    try:
        return scraper.buscar(sesion, **kwargs)
    except scraper.requests.RequestException:
        log.exception("Búsqueda falló, reintento con sesión nueva")
        sesion = _reset_sesion()
        return scraper.buscar(sesion, **kwargs)


# ===================================================================
# Tools
# ===================================================================

@mcp.tool(
    name="buscar_fallos",
    description=(
        "Busca fallos en el portal del PJ Tucumán. Filtros combinables: "
        "descriptores (AND implícito; ≥4 chars por token), carátula "
        "(materia tras 'S/'), partes (actor/demandado), número de sentencia o "
        "expediente, rango de fechas (AMBAS necesarias), tribunal emisor, "
        "solo doctrina legal. Devuelve metadatos + sumario de cada fallo. "
        "IMPORTANTE: numerosos quirks del portal son atajados automáticamente "
        "por el server — leer la respuesta para advertencias y total disponible."
    ),
)
def buscar_fallos(
    descriptores: Annotated[str, Field(
        default="",
        description=(
            "Palabras clave temáticas. AND implícito entre palabras "
            "(ej: 'despido discriminatorio' → fallos con AMBAS). "
            "Comillas para frase exacta. NO soporta OR/AND explícitos. "
            "Tildes y ñ se ignoran. Tokens de ≤3 caracteres se RECHAZAN "
            "(el portal los trata como wildcard)."
        ),
    )] = "",
    caratula: Annotated[str, Field(
        default="",
        description=(
            "Texto del objeto/materia en la carátula (lo que aparece tras 'S/'). "
            "NO busca en partes ni en el cuerpo del fallo. Ej: 'cobro de pesos', "
            "'desalojo'. Para búsqueda temática general usar `descriptores`."
        ),
    )] = "",
    actor: Annotated[str, Field(
        default="",
        description="Nombre o apellido de la parte actora. Búsqueda por substring."
    )] = "",
    demandado: Annotated[str, Field(
        default="",
        description="Nombre o apellido de la parte demandada. Búsqueda por substring."
    )] = "",
    sentencia: Annotated[str, Field(
        default="",
        description=(
            "Número de sentencia (1-5 dígitos). NO es único: se reinicia por "
            "tribunal y año. Combinar con `tribunal` y/o `fecha_desde/hasta` "
            "para localizar un fallo específico."
        ),
    )] = "",
    expte: Annotated[str, Field(
        default="",
        description=(
            "Número de expediente formato 'número/aa' (ej: '572/22'). "
            "Acepta 4 dígitos de año ('572/2022') — se normaliza a 2 dígitos."
        ),
    )] = "",
    fecha_desde: Annotated[str, Field(
        default="",
        description=(
            "Fecha mínima. Formatos aceptados: dd/mm/aaaa, dd-mm-aaaa, "
            "aaaa-mm-dd. Si solo pasás esta sin `fecha_hasta`, el server "
            "autocompleta hoy como hasta. AMBAS necesarias para que el "
            "portal aplique el filtro."
        ),
    )] = "",
    fecha_hasta: Annotated[str, Field(
        default="",
        description=(
            "Fecha máxima. Mismo formato que fecha_desde. Si solo pasás "
            "esta, el server autocompleta 01/01/1990 como desde."
        ),
    )] = "",
    tribunal: Annotated[str, Field(
        default="",
        description=(
            "Nombre del tribunal/cámara/sala EMISOR del fallo. "
            "Acepta cualquier capitalización, tildes y matching parcial. "
            "Ejemplos: 'Cámara del Trabajo', 'Cámara del Trabajo Sala 3', "
            "'Tribunal de Impugnación Capital', 'CORTE SUPREMA DE JUSTICIA'. "
            "Para listar todas las opciones, llamá a `listar_tribunales`. "
            "Si hay ambigüedad, la tool devuelve candidatos."
        ),
    )] = "",
    solo_doctrina_legal: Annotated[bool, Field(
        default=False,
        description=(
            "Si True, restringe a precedentes vinculantes marcados como "
            "Doctrina Legal por la Corte Suprema de Justicia de Tucumán "
            "(~5% del corpus, ~5500 fallos). Útil cuando el usuario "
            "pregunta por jurisprudencia consolidada/vinculante."
        ),
    )] = False,
    limit: Annotated[int, Field(
        default=100, ge=1, le=2000,
        description=(
            "Máximo de fallos a devolver. Default 100. El portal puede "
            "tener miles de matches; el response incluye `total_disponible` "
            "para que sepas cuántos hay realmente."
        ),
    )] = 100,
) -> dict:
    """Búsqueda multi-campo en el portal."""

    # 1) Sanitizar y validar inputs textuales
    try:
        descriptores_s = validators.sanitizar_texto_query(descriptores, nombre="descriptores")
        if descriptores_s:
            validators.validar_token_busqueda(descriptores_s, nombre="descriptores")
        caratula_s = validators.sanitizar_texto_query(caratula, nombre="caratula")
        if caratula_s:
            validators.validar_token_busqueda(caratula_s, nombre="caratula")
        actor_s = validators.sanitizar_texto_query(actor, nombre="actor")
        demandado_s = validators.sanitizar_texto_query(demandado, nombre="demandado")
        sentencia_s = validators.validar_sentencia(sentencia)
        expte_s = validators.normalizar_expte(expte)
        fecha_d, fecha_h = validators.normalizar_rango_fechas(fecha_desde, fecha_hasta)
    except ValueError as e:
        return {"error": str(e), "cantidad": 0, "fallos": []}

    # 2) Validar que haya al menos UN filtro real. solo_doctrina_legal
    # también cuenta como filtro válido (restringe a ~5500 fallos).
    filtros_no_vacios = any([
        descriptores_s.strip() if descriptores_s else "",
        caratula_s.strip() if caratula_s else "",
        actor_s.strip() if actor_s else "",
        demandado_s.strip() if demandado_s else "",
        sentencia_s, expte_s, fecha_d, fecha_h,
        tribunal.strip() if tribunal else "",
        solo_doctrina_legal,
    ])
    if not filtros_no_vacios:
        return {
            "error": (
                "Especificá al menos un filtro (descriptores, carátula, "
                "actor, demandado, sentencia, expediente, fechas, tribunal "
                "o solo_doctrina_legal)."
            ),
            "cantidad": 0, "fallos": [],
        }

    # 3) Resolver tribunal a código
    tribunales_codigos: list[str] = []
    tribunal_resuelto: str | None = None
    if tribunal and tribunal.strip():
        code, err = _resolver_tribunal_o_error(
            tribunal,
            tabla=scraper.TRIBUNALES_DETALLADO,
            nombre_tabla="TRIBUNALES_DETALLADO (79 emisores con sub-salas)",
        )
        if err is not None:
            return err
        tribunales_codigos = [code]
        tribunal_resuelto = scraper.TRIBUNALES_DETALLADO.get(code)

    # 4) Si solo_doctrina_legal=True y no hay descriptores ni otro filtro
    # textual, mandamos descriptores=' ' como wildcard para que el portal
    # devuelva todo el corpus de doctrina legal.
    descriptores_para_portal = descriptores_s
    sin_filtro_textual = not any([
        descriptores_s, caratula_s, actor_s, demandado_s,
        sentencia_s, expte_s, fecha_d, fecha_h, tribunales_codigos,
    ])
    if solo_doctrina_legal and sin_filtro_textual:
        descriptores_para_portal = " "

    try:
        resultado = _ejecutar_busqueda(
            descriptores=descriptores_para_portal,
            causa=caratula_s,
            actor=actor_s,
            demandado=demandado_s,
            sentencia=sentencia_s,
            nexpte=expte_s,
            fechad=fecha_d,
            fechah=fecha_h,
            tribunal=tribunales_codigos,
            doctrina_legal=solo_doctrina_legal,
            limit=limit,
            cantsuma=min(200, limit),
            max_paginas=max(1, (limit + 199) // 200),
        )
    except Exception as e:
        log.exception("Búsqueda crítica falló")
        return {
            "error": f"Error consultando el portal: {e}",
            "cantidad": 0, "fallos": [],
        }

    # 5) Limpieza de descriptores: fallos de doctrina legal traen el prefijo
    # "DOCTRINA LEGAL" concatenado con el descriptor sin separador (ej:
    # "DOCTRINA LEGALSENTENCIA: INVALIDA..."). Lo separamos siempre que
    # el char 15 (después del prefijo de 14) no sea ya un separador.
    _PREFIJO = "DOCTRINA LEGAL"
    fallos_response = []
    for f in resultado.fallos:
        d = _formatear_fallo_para_response(f)
        desc = d.get("descriptores", "")
        if desc.startswith(_PREFIJO):
            resto = desc[len(_PREFIJO):]
            # Si lo que sigue no arranca con espacio/dos puntos/punto, insertamos ": "
            if resto and resto[0] not in " :.\t":
                d["descriptores"] = f"{_PREFIJO}: {resto}"
        fallos_response.append(d)

    response: dict = {
        "cantidad": len(resultado.fallos),
        "total_disponible_en_portal": resultado.total_disponible,
        "tribunal_resuelto": tribunal_resuelto,
        "paginas_recorridas": resultado.paginas_recorridas,
        "corte_por": resultado.corte_por,
        "fallos": fallos_response,
    }
    if resultado.parse_errors:
        response["advertencia_parse_errors"] = resultado.parse_errors

    advertencias: list[str] = []
    LIMIT_MAXIMO = 2000  # debe matchear `le=2000` en el Field
    if (
        resultado.total_disponible
        and resultado.total_disponible > len(resultado.fallos)
    ):
        if limit >= LIMIT_MAXIMO:
            advertencias.append(
                f"Hay {resultado.total_disponible} fallos disponibles pero "
                f"se devolvieron {len(resultado.fallos)} (limit={limit}, "
                f"que es el techo). Refiná tus filtros para acotar."
            )
        else:
            advertencias.append(
                f"Hay {resultado.total_disponible} fallos disponibles pero "
                f"se devolvieron {len(resultado.fallos)} (limit={limit}). "
                f"Pedí limit más alto si necesitás más."
            )
    if resultado.corte_por == "error_http":
        advertencias.append(
            "La lista puede estar incompleta — hubo error HTTP a mitad de la "
            f"paginación: {'; '.join(resultado.errores[:3])}"
        )
    if advertencias:
        response["advertencias"] = advertencias

    return response


@mcp.tool(
    name="obtener_fallo_por_registro",
    description=(
        "Trae un fallo completo (metadata + TEXTO ÍNTEGRO) en una sola "
        "llamada, dado su número de registro. NO requiere búsqueda previa. "
        "Útil cuando ya tenés el número (por cita cruzada, copia desde "
        "otro sistema, o re-consulta de un fallo conocido). El texto se "
        "trunca a un máximo de caracteres con paginación vía offset."
    ),
)
def obtener_fallo_por_registro(
    registro: Annotated[str | int, Field(
        description=(
            "Número de registro (8 dígitos). Acepta con o sin sufijo "
            "(ej: '00078941' o '00078941-01'), como string o entero. "
            "El sufijo indica un sumario individual; el texto íntegro es "
            "el mismo para todos los sufijos del mismo registro base."
        ),
    )],
    max_chars: Annotated[int, Field(
        default=_MAX_CHARS_TEXTO_FALLO, ge=1000, le=200_000,
        description=(
            "Tope de caracteres del texto a devolver. Algunos fallos "
            "exceden 200k chars; truncamos por defecto a 60k. La respuesta "
            "indica `truncado=True` y `longitud_total` para que pidas más "
            "con `offset`."
        ),
    )] = _MAX_CHARS_TEXTO_FALLO,
    offset: Annotated[int, Field(
        default=0, ge=0,
        description="Inicio del rango de caracteres a devolver (para paginar fallos muy largos).",
    )] = 0,
) -> dict:
    """Devuelve el fallo completo dado su número de registro."""
    try:
        reg_base = validators.validar_registro(registro)
    except ValueError as e:
        return {"error": str(e)}

    sesion = _get_sesion()
    try:
        fallo = scraper.obtener_fallo_por_registro(sesion, reg_base)
    except Exception:
        log.exception("Error obtener_fallo_por_registro %s, reintento", reg_base)
        sesion = _reset_sesion()
        try:
            fallo = scraper.obtener_fallo_por_registro(sesion, reg_base)
        except Exception as e:
            return {"error": f"Error consultando el portal: {e}"}

    if fallo is None:
        return {
            "error": (
                f"No encontré el fallo con registro {registro}. Puede ser "
                "que el número sea incorrecto, que el fallo no esté "
                "publicado, o que el portal no haya subido su texto."
            ),
        }

    texto = fallo.texto_completo or ""
    longitud_total = len(texto)
    chunk = texto[offset:offset + max_chars]
    truncado = (offset + len(chunk)) < longitud_total

    return {
        "registro": fallo.registro,
        "tribunal": fallo.tribunal,
        "caratula": fallo.caratula,
        "nro_expte": fallo.nro_expte,
        "nro_sentencia": fallo.nro_sentencia,
        "fecha": fallo.fecha,
        "url": fallo.url,
        "longitud_total_caracteres": longitud_total,
        "offset": offset,
        "longitud_chunk": len(chunk),
        "truncado": truncado,
        "siguiente_offset": (offset + len(chunk)) if truncado else None,
        "texto": chunk,
    }


@mcp.tool(
    name="descargar_texto_fallo",
    description=(
        "[COMPAT v0.1.x] Devuelve solo el texto íntegro de un fallo. "
        "Para más metadata + texto, usar `obtener_fallo_por_registro`."
    ),
)
def descargar_texto_fallo(
    registro: Annotated[str | int, Field(
        description="Número de registro del fallo (ej: '00078941' o '00078941-01')."
    )],
    max_chars: Annotated[int, Field(
        default=_MAX_CHARS_TEXTO_FALLO, ge=1000, le=200_000,
        description="Tope de caracteres del texto a devolver. Default 60000.",
    )] = _MAX_CHARS_TEXTO_FALLO,
    offset: Annotated[int, Field(default=0, ge=0)] = 0,
) -> dict:
    return obtener_fallo_por_registro(registro, max_chars=max_chars, offset=offset)


@mcp.tool(
    name="listar_fallos_recientes",
    description=(
        "Lista los fallos más recientes del portal, opcionalmente filtrando "
        "por tribunal emisor. Útil para 'qué hay nuevo' en un fuero. "
        "Ordenamiento descendente por fecha."
    ),
)
def listar_fallos_recientes(
    tribunal: Annotated[str, Field(
        default="",
        description=(
            "Nombre del tribunal/cámara/sala (opcional, matching parcial). "
            "Vacío = todos. Ejemplos: 'Cámara del Trabajo Sala 3', "
            "'Tribunal de Impugnación Capital'."
        ),
    )] = "",
    limit: Annotated[int, Field(
        default=20, ge=1, le=200,
        description="Cantidad a devolver. Default 20, max 200.",
    )] = 20,
) -> dict:
    tribunales_codigos: list[str] = []
    tribunal_resuelto: str | None = None
    if tribunal and tribunal.strip():
        code, err = _resolver_tribunal_o_error(
            tribunal,
            tabla=scraper.TRIBUNALES_DETALLADO,
            nombre_tabla="TRIBUNALES_DETALLADO",
        )
        if err is not None:
            return err
        tribunales_codigos = [code]
        tribunal_resuelto = scraper.TRIBUNALES_DETALLADO.get(code)

    try:
        # Sin filtros adicionales, el portal exige descriptores=' '
        resultado = _ejecutar_busqueda(
            descriptores="" if tribunales_codigos else " ",
            tribunal=tribunales_codigos,
            limit=limit,
            cantsuma=min(200, limit),
            max_paginas=max(1, (limit + 199) // 200),
        )
    except Exception as e:
        log.exception("Listado reciente falló")
        return {"error": f"Error consultando el portal: {e}", "cantidad": 0, "fallos": []}

    return {
        "cantidad": len(resultado.fallos),
        "total_disponible_en_portal": resultado.total_disponible,
        "tribunal_resuelto": tribunal_resuelto,
        "fallos": [_formatear_fallo_para_response(f) for f in resultado.fallos],
    }


@mcp.tool(
    name="listar_tribunales",
    description=(
        "Devuelve la lista canónica de tribunales soportados. Útil cuando "
        "el usuario menciona un tribunal con nombre ambiguo o querés ver "
        "todas las salas disponibles. La tabla principal es "
        "TRIBUNALES_DETALLADO (79 entradas, códigos de 5 dígitos, "
        "filtro por tribunal EMISOR del fallo). La tabla auxiliar "
        "TRIBUNALES_ORIGEN_CASACION (28 entradas, códigos de 2 dígitos) "
        "se usa raramente — filtra fallos de la CSJT que vinieron por "
        "recurso de casación desde un tribunal específico."
    ),
)
def listar_tribunales() -> dict:
    return {
        "tribunales_detallado": {
            "descripcion": (
                "Filtro principal por tribunal/cámara/sala EMISOR del fallo. "
                "Códigos de 5 dígitos. Usar este para 'buscar fallos de X'."
            ),
            "cantidad": len(scraper.TRIBUNALES_DETALLADO),
            "entradas": [
                {"codigo": c, "nombre": n}
                for c, n in scraper.TRIBUNALES_DETALLADO.items()
            ],
        },
        "tribunales_origen_casacion": {
            "descripcion": (
                "Filtro especial: fallos de la CSJT que vinieron por "
                "recurso de casación desde el tribunal indicado. "
                "Códigos de 2 dígitos. Uso raro."
            ),
            "cantidad": len(scraper.TRIBUNALES_ORIGEN_CASACION),
            "entradas": [
                {"codigo": c, "nombre": n}
                for c, n in scraper.TRIBUNALES_ORIGEN_CASACION.items()
            ],
        },
    }


# ===================================================================
# Entrypoint
# ===================================================================

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Juris Tucumán MCP Server v1.0")
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
        stream=sys.stderr,
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
