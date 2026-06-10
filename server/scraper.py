"""Scraper standalone del portal de jurisprudencia del PJ Tucumán.

Auto-contenido: sin estado en disco, sin caché. Cada llamada hace
requests HTTP frescos. Si el cliente del MCP corre en una IP residencial
argentina (típico en Claude Desktop instalado en PC del usuario), el
portal acepta los requests sin proxy ni túnel.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass

import requests
from bs4 import BeautifulSoup

BASE = "https://juris.justucuman.gov.ar"
URL_FORM = f"{BASE}/busca_juris_internet_new.php"
URL_RES = f"{BASE}/busca_juris_resultado_tabs_new.php"
URL_FALLO = f"{BASE}/mostrar_fallo_tabs.php"


# Lista canónica de tribunales (param `tribunalo` del portal).
# Extraída del <select name="tribunalo"> del formulario. El valor "00"
# significa "todos" y no se manda al portal.
TRIBUNALES: dict[str, str] = {
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


def _normalizar(s: str) -> str:
    """Lowercase, sin tildes, sin signos de puntuación, espacios colapsados."""
    import unicodedata
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    # Cualquier cosa que no sea letra/dígito → espacio (saca paréntesis,
    # comas, puntos, guiones, etc.).
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s.lower()).strip()
    return s


def resolver_tribunal(texto: str) -> tuple[str | None, list[str]]:
    """Resuelve un nombre de tribunal a su código del portal.

    Devuelve (codigo, candidatos). Si hay match único, devuelve el código
    y lista vacía. Si hay ambigüedad o no hay match, devuelve None y la
    lista de nombres candidatos (vacía si no hay ninguno).
    """
    if not texto or not texto.strip():
        return None, []

    # ¿es un código directo de 2 dígitos?
    if texto.strip() in TRIBUNALES:
        return texto.strip(), []

    needle = _normalizar(texto)
    matches: list[tuple[str, str]] = []
    for code, name in TRIBUNALES.items():
        if _normalizar(name) == needle:
            return code, []  # match exacto, terminamos
        if needle in _normalizar(name):
            matches.append((code, name))

    if len(matches) == 1:
        return matches[0][0], []
    if len(matches) > 1:
        return None, [f"{c}: {n}" for c, n in matches]
    return None, []

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9",
    "Referer": URL_FORM,
}


@dataclass
class Fallo:
    """Un fallo del portal con todos sus metadatos básicos."""
    registro: str
    tribunal: str
    caratula: str
    nro_expte: str
    nro_sentencia: str
    fecha: str
    descriptores: str
    sumario: str
    url: str

    def as_dict(self) -> dict:
        return asdict(self)


# ===================================================================
# Sesión HTTP
# ===================================================================

def crear_sesion(proxy: str | None = None) -> requests.Session:
    """Sesión con headers + cookies del portal.

    Si `proxy` es una URL tipo socks5h://host:port o http://user:pass@host:port,
    se aplica. También se lee de SAEJUSBOT_PROXY como fallback.
    """
    sesion = requests.Session()
    sesion.headers.update(HEADERS)
    proxy = proxy or os.environ.get("SAEJUSBOT_PROXY")
    if proxy:
        sesion.proxies = {"http": proxy, "https": proxy}
    # GET inicial para tomar cookies y registrar `session_vistab` para
    # las páginas de detalle del fallo más adelante.
    try:
        sesion.get(URL_FORM, timeout=20)
    except requests.RequestException:
        pass
    return sesion


# ===================================================================
# Búsqueda
# ===================================================================

def buscar(
    sesion: requests.Session,
    *,
    descriptores: str = "", causa: str = "", actor: str = "",
    demandado: str = "", sentencia: str = "", nexpte: str = "",
    fechad: str = "", fechah: str = "", tribunalo: str = "",
    docleg: str = "", max_paginas: int = 3, cantsuma: int = 50,
    sleep_entre_paginas: float = 0.5,
) -> list[Fallo]:
    """Búsqueda paginada en el portal. Devuelve lista de Fallos."""
    fallos: list[Fallo] = []
    total_esperado: int | None = None

    for pag in range(1, max_paginas + 1):
        params = {
            "pag": pag, "bloqueo": "X",
            "descriptores": descriptores, "causa": causa,
            "actor": actor, "demandado": demandado,
            "sentencia": sentencia, "nexpte": nexpte,
            "fechad": fechad, "fechah": fechah,
            "tribunalo": tribunalo, "docleg": docleg,
            "buscopc": "SCT", "publicado": "1",
            "cantsuma": str(cantsuma), "titysuma": "on",
            "vistab": "1", "flagsubmit": "1",
        }
        r = sesion.get(URL_RES, params=params, timeout=30)
        if r.status_code != 200:
            break

        nuevos = _parsear_listado(r.text)
        if not nuevos:
            break
        fallos.extend(nuevos)

        if total_esperado is None:
            m = re.search(r"Registros encontrados:\s*</b>\s*(\d+)", r.text)
            if m:
                total_esperado = int(m.group(1))

        if total_esperado and len(fallos) >= total_esperado:
            break
        if len(nuevos) < cantsuma:
            break
        time.sleep(sleep_entre_paginas)

    return fallos


def _parsear_listado(html: str) -> list[Fallo]:
    """Parsea una página de resultados y devuelve los fallos."""
    soup = BeautifulSoup(html, "html.parser")
    fallos: list[Fallo] = []

    for panel in soup.select("div.panel.panel-"):
        try:
            h3 = panel.find("h3", class_="panel-title")
            strongs = h3.find_all("strong") if h3 else []
            tribunal = strongs[1].get_text(strip=True) if len(strongs) >= 2 else ""

            titulos = panel.find("p", class_="titulos")
            caratula = nro_expte = nro_sentencia = fecha = ""
            if titulos:
                txt = titulos.get_text("\n", strip=True)
                if m := re.search(r"^(S/.+?)(?:\n|Nro)", txt, re.S):
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

            url = (
                f"{URL_FALLO}?registro={registro.split('-')[0]}&vistab=0"
                if registro else ""
            )

            # Descartar placeholders del portal: si no hay registro NI
            # carátula NI fecha, no es un fallo real (es probable que
            # sea un panel "0 resultados" o similar).
            if not registro and not caratula and not fecha:
                continue

            fallos.append(Fallo(
                registro=registro, tribunal=tribunal, caratula=caratula,
                nro_expte=nro_expte, nro_sentencia=nro_sentencia, fecha=fecha,
                descriptores=descriptores, sumario=sumario, url=url,
            ))
        except Exception:
            continue

    return fallos


# ===================================================================
# Texto completo del fallo
# ===================================================================

def descargar_texto_fallo(sesion: requests.Session, registro: str) -> str | None:
    """Baja la página completa del fallo y devuelve el texto íntegro.

    El portal requiere que la sesión tenga `session_vistab` (se setea
    haciendo al menos una búsqueda). Si la primera request viene "vacía",
    hacemos una búsqueda dummy y reintentamos.
    """
    base_reg = registro.split("-")[0]
    url = f"{URL_FALLO}?registro={base_reg}&vistab=0"

    r = sesion.get(url, timeout=30)
    necesita_refresh = (
        "Undefined index" in r.text
        or "session_vistab" in r.text
        or len(r.text) < 20000
    )
    if necesita_refresh:
        # búsqueda dummy para inicializar session_vistab
        sesion.get(URL_RES, params={
            "pag": 1, "bloqueo": "X", "descriptores": "cobro",
            "buscopc": "SCT", "publicado": "1", "cantsuma": "50",
            "titysuma": "on", "vistab": "1", "flagsubmit": "1",
        }, timeout=30)
        r = sesion.get(url, timeout=30)

    soup = BeautifulSoup(r.text, "html.parser")
    pane = soup.select_one("div#red")
    if pane is None:
        return None
    texto = pane.get_text("\n", strip=True)
    return texto if len(texto) > 50 else None
