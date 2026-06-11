"""Scraper standalone del portal de jurisprudencia del PJ Tucumán.

Versión v1.0: rewrite completo con findings del análisis exhaustivo del portal.

Cambios clave vs v0.1.x:
- Sin warmup en crear_sesion() — el GET inicial degradaba carátulas a "S/ X"
- Encoding utf-8 forzado para evitar mojibake en acentos
- Filtro tribunal real (tribunal[] con 79 códigos de 5 dígitos por sala)
- Migración de descargar_texto_fallo a /mostrar_fallo.php (sin session_vistab)
- Carátulas completas con actor y demandado
- Fallback de registro vía <input name="MyGroup">
- Retries con backoff para timeouts y 5xx
- ResultadoBusqueda tipado con corte_por
"""

from __future__ import annotations

import logging
import os
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

BASE = "https://juris.justucuman.gov.ar"
URL_FORM = f"{BASE}/busca_juris_internet_new.php"
URL_RES = f"{BASE}/busca_juris_resultado_tabs_new.php"
URL_FALLO_TABS = f"{BASE}/mostrar_fallo_tabs.php"  # URL pública para listado
URL_FALLO_DIRECTO = f"{BASE}/mostrar_fallo.php"    # para descarga directa


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9",
    "Referer": URL_FORM,
}


# ===================================================================
# Tablas de tribunales
# ===================================================================

# tribunal[] del portal: 79 códigos de 5 dígitos, identifican al
# tribunal EMISOR (incluye sub-salas). Es el filtro semánticamente
# correcto para "buscar fallos de la Cámara del Trabajo".
TRIBUNALES_DETALLADO: dict[str, str] = {
    "10000": "CORTE SUPREMA DE JUSTICIA",
    "10002": "CORTE SUPREMA DE JUSTICIA - Sala Civil y Penal",
    "10003": "CORTE SUPREMA DE JUSTICIA - Sala Laboral y Contencioso Administrativo",
    "10005": "CORTE SUPREMA DE JUSTICIA - Sala en lo Civil y Comercial Común, Civil en Familia y Sucesiones y Penal",
    "10006": "CORTE SUPREMA DE JUSTICIA - Sala en lo Contencioso Administrativo, Laboral, Civil en Documentos y Locaciones y Cobros y Apremios",
    "20100": "CAMARA CIVIL EN FAMILIA y SUCESIONES",
    "20101": "CAMARA CIVIL EN FAMILIA y SUCESIONES - Sala 1",
    "20102": "CAMARA CIVIL EN FAMILIA y SUCESIONES - Sala 2",
    "20103": "CAMARA CIVIL EN FAMILIA y SUCESIONES - Presidencia",
    "20200": "CAMARA CIVIL EN DOCUMENTOS Y LOCACIONES",
    "20201": "CAMARA CIVIL EN DOCUMENTOS Y LOCACIONES - Sala 1",
    "20202": "CAMARA CIVIL EN DOCUMENTOS Y LOCACIONES - Sala 2",
    "20203": "CAMARA CIVIL EN DOCUMENTOS Y LOCACIONES - Sala 3",
    "20204": "CAMARA CIVIL EN DOCUMENTOS Y LOCACIONES - Presidencia",
    "20400": "CAMARA CIVIL Y COMERCIAL COMUN",
    "20401": "CAMARA CIVIL Y COMERCIAL COMUN - Sala 1",
    "20402": "CAMARA CIVIL Y COMERCIAL COMUN - Sala 2",
    "20403": "CAMARA CIVIL Y COMERCIAL COMUN - Sala 3",
    "20404": "CAMARA CIVIL Y COMERCIAL COMUN - Presidencia",
    "20700": "CAMARA DEL TRABAJO",
    "20701": "CAMARA DEL TRABAJO - Sala 1",
    "20702": "CAMARA DEL TRABAJO - Sala 2",
    "20703": "CAMARA DEL TRABAJO - Sala 3",
    "20704": "CAMARA DEL TRABAJO - Sala 4",
    "20705": "CAMARA DEL TRABAJO - Sala 5",
    "20706": "CAMARA DEL TRABAJO - Sala 6",
    "20707": "CAMARA DEL TRABAJO - Presidencia",
    "20800": "CAMARA DE APELACIONES EN LO PENAL DE INSTRUCCION",
    "20801": "CAMARA DE APELACIONES EN LO PENAL DE INSTRUCCION - Sala Unica",
    "20900": "CAMARA PENAL",
    "20901": "CAMARA PENAL - Sala 1",
    "20902": "CAMARA PENAL - Sala 2",
    "20903": "CAMARA PENAL - Sala 3",
    "20904": "CAMARA PENAL - Sala 4",
    "20905": "CAMARA PENAL - Sala 5",
    "20906": "CAMARA PENAL - Sala 6",
    "21001": "CAMARA PENAL - CONCLUSIONAL - Sala 1",
    "21002": "CAMARA PENAL - CONCLUSIONAL - Sala 2",
    "21003": "CAMARA PENAL - CONCLUSIONAL - Sala 3",
    "21004": "CAMARA PENAL - CONCLUSIONAL - Sala 4",
    "21005": "CAMARA PENAL - CONCLUSIONAL - Sala 5",
    "21100": "CAMARA EN LO CONTENCIOSO ADMINISTRATIVO",
    "21101": "CAMARA EN LO CONTENCIOSO ADMINISTRATIVO - Sala 1",
    "21102": "CAMARA EN LO CONTENCIOSO ADMINISTRATIVO - Sala 2",
    "21103": "CAMARA EN LO CONTENCIOSO ADMINISTRATIVO - Sala 3",
    "21201": "TRIBUNAL DE IMPUGNACION (Capital)",
    "21301": "COLEGIO DE JUECES (Capital)",
    "22100": "CAMARA CIVIL Y COMERCIAL COMUN - CONCEPCION",
    "22101": "CAMARA CIVIL Y COMERCIAL COMUN - CONCEPCION - Sala Unica",
    "22102": "CAMARA CIVIL Y COMERCIAL COMUN - CONCEPCION - Sala 1",
    "22103": "CAMARA CIVIL Y COMERCIAL COMUN - CONCEPCION - Sala 2",
    "22200": "CAMARA CIVIL EN DOC. Y LOC. Y FLIA. Y SUCESIONES - CONCEPCION",
    "22201": "CAMARA CIVIL EN DOC. Y LOC. Y FLIA. Y SUCESIONES - CONCEPCION - Sala en lo Civil en Familia y Sucesiones",
    "22202": "CAMARA CIVIL EN DOC. Y LOC. Y FLIA. Y SUCESIONES - CONCEPCION - Sala en lo Civil en Documentos y Locaciones",
    "22300": "CAMARA PENAL - CONCEPCION",
    "22301": "CAMARA PENAL - CONCEPCION - Sala 1",
    "22302": "CAMARA PENAL - CONCEPCION - Sala 2",
    "22400": "CAMARA DEL TRABAJO - CONCEPCION",
    "22401": "CAMARA DEL TRABAJO - CONCEPCION - Sala 1",
    "22402": "CAMARA DEL TRABAJO - CONCEPCION - Sala 2",
    "22500": "CAMARA DE FERIA",
    "22501": "CAMARA DE FERIA - Penal, Correccional y de Menores",
    "22502": "CAMARA DE FERIA - Civil, del Trabajo, Cont. Adm., Doc. y Loc. y Flia y Suc.",
    "22601": "TRIBUNAL DE IMPUGNACION (Concepcion y Monteros)",
    "22701": "COLEGIO DE JUECES (Concepcion)",
    "22800": "CAMARA DE FERIA TRIBUNAL CONCLUSIONAL",
    "22900": "CAMARA DE FERIA TRIBUNAL DE IMPUGNACION",
    "23000": "CAMARA DE FERIA COLEGIO DE JUECES",
    "23200": "CAMARA PENAL CONCLUSIONAL APELACIONES",
    "23201": "CAMARA PENAL CONCLUSIONAL APELACIONES - Sala 1",
    "23202": "CAMARA PENAL CONCLUSIONAL APELACIONES - Sala 2",
    "23203": "CAMARA PENAL CONCLUSIONAL APELACIONES - Sala 3",
    "23204": "CAMARA PENAL CONCLUSIONAL APELACIONES - Sala 4",
    "23205": "CAMARA PENAL CONCLUSIONAL APELACIONES - Sala 5",
    "23600": "CAMARA CIVIL EN FAMILIA Y SUCESIONES (C.J.E.)",
    "23602": "CAMARA CIVIL EN FAMILIA Y SUCESIONES (C.J.E.) - Presidencia",
    "23603": "CAMARA CIVIL EN FAMILIA Y SUCESIONES (C.J.E.) - Sala 1",
    "23604": "CAMARA CIVIL EN FAMILIA Y SUCESIONES (C.J.E.) - Sala 2",
}


# tribunalo del portal: 28 códigos de 2 dígitos. Es el "Tribunal de Origen
# del recurso de casación" — sólo aplica a fallos de la CSJT que vinieron
# por casación. NO es el filtro común de "buscar fallos de Cámara X".
TRIBUNALES_ORIGEN_CASACION: dict[str, str] = {
    "02": "CAMARA CIVIL EN FAMILIA y SUCESIONES",
    "03": "CAMARA CIVIL EN DOCUMENTOS Y LOCACIONES",
    "04": "CAMARA CIVIL Y COMERCIAL COMUN",
    "05": "CAMARA DEL TRABAJO",
    "06": "CAMARA PENAL",
    "07": "CAMARA EN LO CONTENCIOSO ADMINISTRATIVO",
    "08": "CAMARA CIVIL Y COMERCIAL COMUN - CONCEPCION",
    "09": "CAMARA CIVIL EN DOC. Y LOC. Y FLIA. Y SUCESIONES - CONCEPCION",
    "10": "CAMARA PENAL - CONCEPCION",
    "11": "CAMARA DEL TRABAJO - CONCEPCION",
    "12": "CAMARA DE FERIA",
    "13": "CAMARA DE APELACIONES EN LO PENAL DE INSTRUCCION",
    "14": "JUZGADOS CORRECCIONALES",
    "15": "JUZGADOS DE INSTRUCCION",
    "16": "JUZGADOS CORRECCIONALES - CONCEPCION",
    "17": "JUZGADOS DE INSTRUCCION - CONCEPCION",
    "18": "JUZGADOS DE INSTRUCCION - MONTEROS",
    "19": "CAMARA PENAL - CONCLUSIONAL",
    "20": "TRIBUNAL DE IMPUGNACION (Capital)",
    "21": "COLEGIO DE JUECES (Capital)",
    "22": "TRIBUNAL DE IMPUGNACION (Concepcion y Monteros)",
    "23": "COLEGIO DE JUECES (Concepcion)",
    "31": "JUZGADOS DE INSTRUCCION CONCLUSIONAL",
    "32": "CAMARA PENAL CONCLUSIONAL APELACIONES",
    "33": "SECRETARIA CONTRAVENCIONAL",
    "34": "CAMARA CIVIL EN DOC. Y LOCACIONES Y FAMILIA Y SUCES. CONCE - Sala en lo Civil en Familia y Sucesiones",
    "35": "CAMARA CIVIL EN DOC. Y LOCACIONES Y FAMILIA Y SUCES. CONCE - Sala en lo Civil en Documentos y Locaciones",
    "36": "CAMARA CIVIL EN FAMILIA Y SUCESIONES (C.J.E.)",
}

# Alias para retrocompatibilidad
TRIBUNALES = TRIBUNALES_DETALLADO


def _normalizar(s: str) -> str:
    """Lowercase, sin tildes, sin puntuación, espacios colapsados."""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s.lower()).strip()
    return s


# Aliases comunes (versión normalizada → código TRIBUNALES_DETALLADO)
_TRIBUNAL_ALIASES: dict[str, str] = {
    "csj": "10000",
    "csjt": "10000",
    "corte suprema": "10000",
    "corte suprema de tucuman": "10000",
    "corte suprema de justicia de tucuman": "10000",
    "corte": "10000",
    "ti capital": "21201",
    "ti concepcion": "22601",
    "ti capital tucuman": "21201",
    "tribunal impugnacion capital": "21201",
    "tribunal impugnacion concepcion": "22601",
}

# Tokens geográficos que actúan como filtro EXCLUSIVO (no inclusivo)
_TOKENS_GEO_CAPITAL = {"capital", "smt", "san miguel"}
_TOKENS_GEO_CONCEPCION = {"concepcion", "concepción"}
_TOKENS_GEO_MONTEROS = {"monteros"}


def _es_concepcion(name_normalizado: str) -> bool:
    return "concepcion" in name_normalizado


def _es_cje(name_normalizado: str) -> bool:
    return "cje" in name_normalizado or "c j e" in name_normalizado


_MAX_CANDIDATOS_RESPONSE = 12


def resolver_tribunal(
    texto: str, *, tabla: dict[str, str] | None = None
) -> tuple[str | None, list[str]]:
    """Resuelve un nombre de tribunal a su código del portal.

    Mejoras v1.0:
    - Aliases comunes (CSJ, CSJT, etc.)
    - Matching por set de tokens (no por substring contiguo)
    - Reconoce "Capital" como filtro exclusivo de no-Concepción
    - Fuzzy matching para typos (difflib)
    - Tope de candidatos
    - Cuando el código del usuario es de TRIBUNALES_ORIGEN_CASACION, da
      pista en lugar de fallar silenciosamente.

    Devuelve (codigo, candidatos). Match único → (codigo, []).
    Ambigüedad o sin match con candidatos cercanos → (None, [str_descriptivos]).
    """
    if tabla is None:
        tabla = TRIBUNALES_DETALLADO
    if not texto or not texto.strip():
        return None, []
    texto = texto.strip()

    # 1) Código directo en la tabla pedida
    if texto in tabla:
        return texto, []

    # 2) Si el texto es código pero pertenece a la OTRA tabla, mensaje específico
    if tabla is TRIBUNALES_DETALLADO and texto in TRIBUNALES_ORIGEN_CASACION:
        return None, [
            f"El código '{texto}' corresponde a tribunales_origen_casacion "
            f"(filtro especial para fallos de CSJT por casación). "
            f"Para el filtro principal usá un código de 5 dígitos "
            f"(ej: {next(iter(TRIBUNALES_DETALLADO))})."
        ]

    needle_norm = _normalizar(texto)

    # 3) Aliases comunes (solo para tabla detallada)
    if tabla is TRIBUNALES_DETALLADO and needle_norm in _TRIBUNAL_ALIASES:
        return _TRIBUNAL_ALIASES[needle_norm], []

    # 4) Detectar tokens geográficos para filtrado exclusivo
    tokens_input = set(needle_norm.split())
    quiere_capital = bool(tokens_input & _TOKENS_GEO_CAPITAL)
    quiere_concepcion = bool(tokens_input & _TOKENS_GEO_CONCEPCION)
    quiere_monteros = bool(tokens_input & _TOKENS_GEO_MONTEROS)

    # Sacamos los tokens geo del needle para el matching textual
    tokens_busqueda = (
        tokens_input
        - _TOKENS_GEO_CAPITAL
        - _TOKENS_GEO_CONCEPCION
        - _TOKENS_GEO_MONTEROS
    )

    # 5) Matching por subset de tokens
    matches_exactos: list[tuple[str, str]] = []
    matches_parciales: list[tuple[str, str]] = []
    for code, name in tabla.items():
        name_norm = _normalizar(name)
        tokens_name = set(name_norm.split())

        if name_norm == needle_norm:
            return code, []

        # Aplicar filtros geográficos como exclusivos
        es_concepcion = _es_concepcion(name_norm)
        es_cje = _es_cje(name_norm)

        if quiere_capital and (es_concepcion or "monteros" in name_norm):
            continue
        if quiere_concepcion and not es_concepcion:
            continue
        if quiere_monteros and "monteros" not in name_norm:
            continue

        # Si el usuario pidió "Capital" y este tribunal es CJE (juzgado),
        # NO es lo que típicamente quiere. Lo bajamos a parcial.
        prioridad_baja = quiere_capital and es_cje

        if tokens_busqueda and tokens_busqueda.issubset(tokens_name):
            if prioridad_baja:
                matches_parciales.append((code, name))
            else:
                matches_exactos.append((code, name))
        elif tokens_busqueda and needle_norm in name_norm:
            # match por substring contiguo (fallback compat)
            matches_parciales.append((code, name))

    matches = matches_exactos if matches_exactos else matches_parciales

    if len(matches) == 1:
        return matches[0][0], []
    if len(matches) > 1:
        # Si hay UN match cuyo nombre normalizado IGUALA al needle (sin tokens geo),
        # preferirlo como exacto en lugar de ambigüedad.
        exactos_nombre = [
            (c, n) for c, n in matches
            if set(_normalizar(n).split()) == set(tokens_busqueda)
        ]
        if len(exactos_nombre) == 1:
            return exactos_nombre[0][0], []

        candidatos = [f"{c}: {n}" for c, n in matches]
        if len(candidatos) > _MAX_CANDIDATOS_RESPONSE:
            total = len(candidatos)
            candidatos = (
                candidatos[:_MAX_CANDIDATOS_RESPONSE]
                + [f"... +{total - _MAX_CANDIDATOS_RESPONSE} más; refiná la búsqueda"]
            )
        return None, candidatos

    # 6) Sin match: fuzzy fallback con difflib
    import difflib
    nombres_norm = {_normalizar(n): (c, n) for c, n in tabla.items()}
    similares = difflib.get_close_matches(
        needle_norm, list(nombres_norm.keys()), n=5, cutoff=0.65,
    )
    if similares:
        candidatos = [
            f"{nombres_norm[s][0]}: {nombres_norm[s][1]} (¿quisiste decir esto?)"
            for s in similares
        ]
        return None, candidatos
    return None, []


# ===================================================================
# Modelos
# ===================================================================

@dataclass
class Fallo:
    """Un fallo con metadatos completos. `texto_completo` es opcional."""
    registro: str          # ej. "00077887-01" (con sufijo de sumario)
    tribunal: str
    caratula: str          # carátula completa: "ACTOR Vs. DEMANDADO S/ MATERIA"
    nro_expte: str
    nro_sentencia: str
    fecha: str             # dd/mm/yyyy
    descriptores: str
    sumario: str
    url: str
    texto_completo: str = ""  # poblado si se llamó obtener_fallo_por_registro

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ResultadoBusqueda:
    fallos: list[Fallo]
    total_disponible: int | None  # total reportado por el portal
    paginas_recorridas: int
    corte_por: str  # 'fin' | 'max_paginas' | 'total_alcanzado' | 'error_http' | 'sin_resultados' | 'limit_alcanzado'
    parse_errors: int = 0
    errores: list[str] = field(default_factory=list)


# ===================================================================
# Sesión HTTP
# ===================================================================

def crear_sesion(proxy: str | None = None) -> requests.Session:
    """Crea sesión con headers, retries y proxy opcional.

    A diferencia de v0.1.x, NO hace GET inicial al formulario. Ese GET
    degradaba las carátulas de búsquedas posteriores a `S/ DESPIDO`
    perdiendo actor y demandado. Las cookies se setean implícitamente
    en la primera búsqueda. Para descargar_texto_fallo() se usa
    /mostrar_fallo.php que no requiere `session_vistab`.
    """
    sesion = requests.Session()
    sesion.headers.update(HEADERS)

    # Retries con backoff exponencial para 5xx y timeouts transitorios
    retry = Retry(
        total=3,
        backoff_factor=1.5,  # 1.5, 3, 6 segundos
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset(("GET",)),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    sesion.mount("http://", adapter)
    sesion.mount("https://", adapter)

    proxy = proxy or os.environ.get("SAEJUSBOT_PROXY")
    if proxy:
        sesion.proxies = {"http": proxy, "https": proxy}
    return sesion


# ===================================================================
# Búsqueda
# ===================================================================

def buscar(
    sesion: requests.Session,
    *,
    descriptores: str = "",
    causa: str = "",
    actor: str = "",
    demandado: str = "",
    sentencia: str = "",
    nexpte: str = "",
    fechad: str = "",
    fechah: str = "",
    tribunal: list[str] | str = "",  # códigos de 5 dígitos (tribunal[])
    tribunal_origen_casacion: str = "",  # código de 2 dígitos (tribunalo)
    doctrina_legal: bool = False,  # docleg=1
    max_paginas: int = 3,
    cantsuma: int = 100,
    limit: int | None = None,
    sleep_entre_paginas: float = 0.4,
    timeout: int = 60,
) -> ResultadoBusqueda:
    """Búsqueda paginada en el portal.

    Args:
        descriptores: palabras clave (AND implícito, frases con comillas)
        causa: texto en la carátula (parte S/...)
        actor, demandado: nombres de partes
        sentencia: número de sentencia (no único — combinar con tribunal/fecha)
        nexpte: número de expediente formato 'número/aa'
        fechad, fechah: rango dd/mm/yyyy. AMBAS necesarias en el portal.
        tribunal: lista de códigos de 5 dígitos (TRIBUNALES_DETALLADO).
                  Acepta string single o list de strings. Filtro REAL
                  por tribunal emisor.
        tribunal_origen_casacion: código de 2 dígitos (TRIBUNALES_ORIGEN_CASACION).
                  Filtro especial para fallos de la CSJT que vinieron
                  por casación desde un tribunal específico.
        doctrina_legal: si True, restringe a precedentes vinculantes de la CSJT.
        max_paginas: tope superior de páginas a recorrer.
        cantsuma: resultados por página (default 100, el portal acepta hasta 500).
        limit: tope superior absoluto de fallos a devolver.

    Returns: ResultadoBusqueda con fallos, total reportado, corte_por, errores.
    """
    # Normalizar tribunal a lista
    if isinstance(tribunal, str):
        tribunales = [tribunal] if tribunal else []
    else:
        tribunales = list(tribunal)
    tribunales = [t for t in tribunales if t]

    fallos: list[Fallo] = []
    total_disponible: int | None = None
    parse_errors = 0
    errores: list[str] = []
    corte_por = "fin"
    pag = 0

    for pag in range(1, max_paginas + 1):
        params: dict = {
            "pag": pag,
            "bloqueo": "X",
            "descriptores": descriptores,
            "causa": causa,
            "actor": actor,
            "demandado": demandado,
            "sentencia": sentencia,
            "nexpte": nexpte,
            "fechad": fechad,
            "fechah": fechah,
            "tribunalo": tribunal_origen_casacion,
            "docleg": "1" if doctrina_legal else "",
            "buscopc": "SCT",
            "publicado": "1",
            "cantsuma": str(cantsuma),
            "titysuma": "on",
            "vistab": "1",
            "flagsubmit": "1",
        }
        # Multi-valor: requests con list value genera tribunal[]=A&tribunal[]=B
        if tribunales:
            params["tribunal[]"] = tribunales

        # Drop empty values para reducir ruido
        params = {k: v for k, v in params.items() if v not in (None, "")}

        try:
            r = sesion.get(URL_RES, params=params, timeout=timeout)
        except requests.RequestException as e:
            errores.append(f"pag {pag}: {type(e).__name__}: {e}")
            corte_por = "error_http"
            break

        if r.status_code != 200:
            errores.append(f"pag {pag}: HTTP {r.status_code}")
            corte_por = "error_http"
            break

        # Forzar UTF-8 (el portal a veces no manda charset en el header)
        r.encoding = "utf-8"

        if total_disponible is None:
            m = re.search(r"Registros encontrados:\s*(\d+)", r.text)
            if m:
                total_disponible = int(m.group(1))
                log.debug("Total disponible reportado: %d", total_disponible)

        nuevos, errs = _parsear_listado(r.text)
        parse_errors += errs
        if not nuevos:
            corte_por = "sin_resultados" if pag == 1 else "fin"
            break

        fallos.extend(nuevos)

        if limit is not None and len(fallos) >= limit:
            fallos = fallos[:limit]
            corte_por = "limit_alcanzado"
            break

        if total_disponible is not None and len(fallos) >= total_disponible:
            corte_por = "total_alcanzado"
            break

        if len(nuevos) < cantsuma:
            corte_por = "fin"
            break

        if pag == max_paginas:
            corte_por = "max_paginas"

        time.sleep(sleep_entre_paginas)

    return ResultadoBusqueda(
        fallos=fallos,
        total_disponible=total_disponible,
        paginas_recorridas=pag,
        corte_por=corte_por,
        parse_errors=parse_errors,
        errores=errores,
    )


def _parsear_listado(html: str) -> tuple[list[Fallo], int]:
    """Devuelve (fallos, n_errores) de una página de resultados."""
    soup = BeautifulSoup(html, "html.parser")
    fallos: list[Fallo] = []
    errores = 0

    for panel in soup.select("div.panel.panel-"):
        try:
            f = _parsear_panel(panel)
            if f is not None:
                fallos.append(f)
        except Exception as e:
            errores += 1
            log.warning("Panel saltado: %s", e, exc_info=False)
            continue

    return fallos, errores


def _parsear_panel(panel) -> Fallo | None:
    """Parsea UN panel del listado. Devuelve None si es un placeholder."""
    h3 = panel.find("h3", class_="panel-title")
    strongs = h3.find_all("strong") if h3 else []
    tribunal = strongs[1].get_text(strip=True) if len(strongs) >= 2 else ""

    titulos = panel.find("p", class_="titulos")
    caratula = nro_expte = nro_sentencia = fecha = ""
    if titulos:
        txt = titulos.get_text("\n", strip=True)
        # Carátula: TODA la primera línea (incluye actor, "Vs.", demandado, S/ MATERIA).
        # Antes era solo "S/ MATERIA" por el regex incorrecto.
        if m := re.search(r"^([^\n]+?)(?:\nNro\.|\Z)", txt, re.S):
            caratula = m.group(1).strip()
        if m := re.search(r"Nro\.\s*Expte:\s*([^\n]+)", txt):
            nro_expte = m.group(1).strip()
        if m := re.search(r"Nro\.\s*Sent:\s*(\S+)", txt):
            nro_sentencia = m.group(1).strip()
        if m := re.search(r"Fecha Sentencia\s*([\d/]+)", txt):
            fecha = m.group(1).strip()

    tab = panel.find("div", class_="tab-pane")
    descriptores = sumario = registro = ""
    if tab:
        p_desc = tab.find("p")
        if p_desc and p_desc.find("strong"):
            descriptores = p_desc.find("strong").get_text(strip=True)

        p_sum = tab.find("p", class_="sumario")
        if p_sum:
            b = p_sum.find("b")
            if b and "Registro" in b.get_text():
                span = b.find("span", class_="numerofecha")
                if span:
                    registro = span.get_text(strip=True)
                b.decompose()
            sumario = p_sum.get_text(" ", strip=True)

    # Fallback: si no encontramos el registro, lo buscamos en el <input name="MyGroup">
    # que el portal incluye para el "carrito de selección". Valor formato '0007890501'
    # (10 dígitos concatenados, sin guión).
    if not registro:
        inp = panel.find("input", attrs={"name": "MyGroup"})
        if inp and inp.get("value"):
            v = inp["value"]
            if re.fullmatch(r"\d{10}", v):
                # 8 dígitos de registro + 2 dígitos de sufijo de sumario
                registro = f"{v[:8]}-{v[8:]}"

    base_reg = registro.split("-")[0] if registro else ""
    url = f"{URL_FALLO_TABS}?registro={base_reg}&vistab=0" if base_reg else ""

    # Descartar placeholders del portal: si no hay ni registro ni carátula
    # ni fecha, no es un fallo real.
    if not registro and not caratula and not fecha:
        return None

    return Fallo(
        registro=registro, tribunal=tribunal, caratula=caratula,
        nro_expte=nro_expte, nro_sentencia=nro_sentencia, fecha=fecha,
        descriptores=descriptores, sumario=sumario, url=url,
    )


# ===================================================================
# Descarga del fallo individual
# ===================================================================

def _limpiar_texto(texto: str) -> str:
    """Saca CRLF, basura de tabs, colapsa líneas vacías repetidas."""
    if not texto:
        return ""
    texto = texto.replace("\r", "")
    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r" *\n *", "\n", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    # Sacar "Fallo\n" inicial si vino del título del tab
    while texto.startswith("Fallo\n") or texto.startswith("FALLO\n"):
        texto = texto.split("\n", 1)[1] if "\n" in texto else ""
    return texto.strip()


def _parsear_pagina_fallo(html: str) -> dict | None:
    """Parsea el HTML de /mostrar_fallo.php → dict con metadata + texto.

    Devuelve None si el HTML no contiene un fallo válido.
    """
    soup = BeautifulSoup(html, "html.parser")
    panel = soup.select_one("div#contenido div.panel")
    if not panel:
        return None
    head = panel.select_one(".panel-heading")
    body = panel.select_one(".panel-body")
    if not body:
        return None

    texto = _limpiar_texto(body.get_text("\n", strip=True))
    if len(texto) < 50:
        return None

    # Parsear el heading (líneas separadas por <br>)
    metadata: dict = {
        "tribunal": "", "caratula": "", "nro_expte": "",
        "nro_sentencia": "", "fecha": "", "registro": "",
    }
    if head:
        lineas = [
            ln.strip()
            for ln in head.get_text("\n", strip=True).split("\n")
            if ln.strip()
        ]
        # heading típico (cada línea de <br>):
        # 0: CAMARA DEL TRABAJO - CONCEPCION - Sala 2
        # 1: S/ DESPIDO  (o "ACTOR Vs. DEMANDADO S/ MATERIA")
        # 2: Nro. Expte: 22/25
        # 3: Nro. Sent: 316 Fecha Sentencia: 29/04/2026
        # 4: Registro: 00078941
        for ln in lineas:
            if m := re.match(r"Nro\.\s*Expte:\s*(.+)", ln, re.IGNORECASE):
                metadata["nro_expte"] = m.group(1).strip()
            elif m := re.match(
                # El portal usa "Fecha Sentencia:" (con dos puntos opcionales).
                # Antes el regex pedía espacio, perdía sentencia + fecha.
                r"Nro\.\s*Sent:?\s*(\S+)\s+Fecha\s+Sentencia[:\s]+([\d/]+)",
                ln, re.IGNORECASE,
            ):
                metadata["nro_sentencia"] = m.group(1).strip()
                metadata["fecha"] = m.group(2).strip()
            elif m := re.match(r"Registro:?\s*(\S+)", ln, re.IGNORECASE):
                metadata["registro"] = m.group(1).strip()
            elif metadata["tribunal"] == "":
                metadata["tribunal"] = ln
            elif metadata["caratula"] == "" and ("S/" in ln or "Vs." in ln.upper()):
                metadata["caratula"] = ln

    metadata["texto"] = texto
    return metadata


def obtener_fallo_por_registro(
    sesion: requests.Session, registro: str, *, timeout: int = 30,
) -> Fallo | None:
    """Trae el fallo completo (metadata + texto íntegro) en una request.

    Usa /mostrar_fallo.php (sin warmup, sin session_vistab). Devuelve
    Fallo o None si no existe / no parsea.
    """
    base_reg = registro.split("-")[0].lstrip("0") or "0"
    base_reg = base_reg.zfill(8)
    url = f"{URL_FALLO_DIRECTO}?registro={base_reg}"

    try:
        r = sesion.get(url, timeout=timeout)
    except requests.RequestException as e:
        log.warning("Error HTTP en obtener_fallo %s: %s", registro, e)
        return None

    if r.status_code != 200:
        log.warning("obtener_fallo %s: HTTP %d", registro, r.status_code)
        return None

    r.encoding = "utf-8"
    data = _parsear_pagina_fallo(r.text)
    if data is None:
        return None

    return Fallo(
        registro=data["registro"] or base_reg,
        tribunal=data["tribunal"],
        caratula=data["caratula"],
        nro_expte=data["nro_expte"],
        nro_sentencia=data["nro_sentencia"],
        fecha=data["fecha"],
        descriptores="",
        sumario="",
        url=f"{URL_FALLO_TABS}?registro={base_reg}&vistab=0",
        texto_completo=data["texto"],
    )


def descargar_texto_fallo(
    sesion: requests.Session, registro: str, *, timeout: int = 30,
) -> str | None:
    """Compat shim: devuelve solo el texto. Internamente usa /mostrar_fallo.php.

    Deprecated: usar obtener_fallo_por_registro() para tener también metadata.
    """
    fallo = obtener_fallo_por_registro(sesion, registro, timeout=timeout)
    return fallo.texto_completo if fallo else None
