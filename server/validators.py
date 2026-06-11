"""Validadores y normalizadores de inputs antes de mandarlos al portal.

El portal del PJ Tucumán falla silenciosamente cuando los inputs vienen
mal formados: si una fecha es inválida o si falta `fechad` cuando hay
`fechah`, devuelve el corpus completo (120k+ fallos) como si fuera el
resultado. Acá validamos y normalizamos para evitar esos falsos positivos.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata


_FECHA_PARSERS: list[tuple[str, str]] = [
    # (formato strptime, ejemplo). Año DEBE ser de 4 dígitos para evitar
    # ambigüedad del cutoff %y (Python 00-68→20xx, 69-99→19xx).
    ("%d/%m/%Y", "31/12/2025"),
    ("%d-%m-%Y", "31-12-2025"),
    ("%Y-%m-%d", "2025-12-31"),
    ("%Y/%m/%d", "2025/12/31"),
]

FORMATOS_ACEPTADOS_TXT = "dd/mm/aaaa, dd-mm-aaaa, aaaa-mm-dd o aaaa/mm/dd"
FECHA_MIN = dt.date(1990, 1, 1)


def normalizar_fecha(s: str, *, nombre: str = "fecha") -> str:
    """Acepta varios formatos, devuelve siempre 'dd/mm/yyyy' (zero-padded).

    Lanza ValueError con mensaje claro si no parsea o si está fuera de rango.
    Año de 2 dígitos NO se acepta (ambigüedad con cutoff Python).
    """
    if not s or not s.strip():
        return ""
    s = s.strip()
    # Rechazar año de 2 dígitos explícitamente
    if re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{2}", s):
        raise ValueError(
            f"{nombre} '{s}' usa año de 2 dígitos (ambiguo). "
            f"Usá año de 4 dígitos: 31/12/2025 en lugar de 31/12/25."
        )
    for fmt, _ej in _FECHA_PARSERS:
        try:
            d = dt.datetime.strptime(s, fmt).date()
            break
        except ValueError:
            continue
    else:
        raise ValueError(
            f"{nombre} '{s}' no es una fecha válida. "
            f"Formatos aceptados: {FORMATOS_ACEPTADOS_TXT} (ej: 31/12/2025)."
        )

    if d < FECHA_MIN:
        raise ValueError(
            f"{nombre} {d:%d/%m/%Y} es demasiado vieja (mínimo {FECHA_MIN:%d/%m/%Y})."
        )
    hoy = dt.date.today()
    if d > hoy + dt.timedelta(days=365):
        raise ValueError(
            f"{nombre} {d:%d/%m/%Y} es demasiado al futuro."
        )
    return d.strftime("%d/%m/%Y")


def normalizar_rango_fechas(
    fecha_desde: str, fecha_hasta: str
) -> tuple[str, str]:
    """Devuelve (desde, hasta) normalizadas. Autocompleta el extremo faltante.

    El portal sólo aplica el filtro si AMBAS fechas están presentes. Si
    solo viene una, autocompletamos la otra de manera conservadora (1 año
    de ventana en vez de 35 años) para que el filtro sea útil.
    """
    desde = normalizar_fecha(fecha_desde, nombre="fecha_desde")
    hasta = normalizar_fecha(fecha_hasta, nombre="fecha_hasta")

    if desde and not hasta:
        # Autocompletar 1 año hacia adelante (no hoy si la desde es vieja)
        dd = dt.datetime.strptime(desde, "%d/%m/%Y").date()
        hasta_auto = min(dd.replace(year=dd.year + 1), dt.date.today())
        hasta = hasta_auto.strftime("%d/%m/%Y")
    if hasta and not desde:
        # Autocompletar 1 año hacia atrás (no 1990)
        dh = dt.datetime.strptime(hasta, "%d/%m/%Y").date()
        try:
            desde_auto = dh.replace(year=dh.year - 1)
        except ValueError:
            desde_auto = dh - dt.timedelta(days=365)
        desde_auto = max(desde_auto, FECHA_MIN)
        desde = desde_auto.strftime("%d/%m/%Y")

    if desde and hasta:
        dd = dt.datetime.strptime(desde, "%d/%m/%Y").date()
        dh = dt.datetime.strptime(hasta, "%d/%m/%Y").date()
        if dd > dh:
            raise ValueError(
                f"fecha_desde ({desde}) es posterior a fecha_hasta ({hasta})."
            )
    return desde, hasta


def validar_registro(registro) -> str:
    """Limpia y valida un número de registro de fallo.

    Acepta '00077887', '00077887-01', '77887', '77887-1', o incluso int.
    Devuelve la forma base (sin sufijo, padding a 8 dígitos).
    """
    # Aceptar int como conveniencia y convertir a str
    if isinstance(registro, int):
        registro = str(registro)
    if not isinstance(registro, str):
        raise ValueError(
            f"registro debe ser string o entero, "
            f"recibido {type(registro).__name__}."
        )
    s = registro.strip()
    if not s:
        raise ValueError("registro vacío.")
    # Separar base y sufijo
    partes = s.split("-")
    base = partes[0]
    sufijo = partes[1] if len(partes) > 1 else ""
    if not re.fullmatch(r"\d{1,10}", base):
        raise ValueError(
            f"registro '{registro}' inválido. Debe ser numérico, "
            f"con o sin sufijo (ej: '00077887' o '00077887-01')."
        )
    # Si vino sufijo, validar que sea 1-2 dígitos
    if sufijo and not re.fullmatch(r"\d{1,2}", sufijo):
        raise ValueError(
            f"registro '{registro}' tiene sufijo inválido '{sufijo}'. "
            f"Debe ser de 1-2 dígitos (ej: '00077887-01')."
        )
    # Padding a 8 dígitos por convención del portal
    return base.zfill(8)


def validar_sentencia(sentencia: str) -> str:
    """Valida un número de sentencia (1..5 dígitos, no cero).

    Rechaza '0', '00', '00000': el portal trata cero como wildcard y
    devuelve el corpus completo (silent failure).
    """
    if not sentencia or not sentencia.strip():
        return ""
    s = sentencia.strip().lstrip("0") or "0"
    if not re.fullmatch(r"\d{1,5}", s):
        raise ValueError(
            f"sentencia '{sentencia}' inválida. Debe ser numérica de hasta 5 dígitos."
        )
    if s == "0":
        raise ValueError(
            f"sentencia '{sentencia}' inválida: el portal trata cero como "
            f"wildcard y devuelve el catálogo completo. Usá un número >= 1."
        )
    return s


def normalizar_expte(expte: str) -> str:
    """Normaliza un número de expediente al formato 'numero/aa' (año 2 dígitos).

    Acepta '572/22', '572/2022', '00572/22'. El portal sólo matchea con
    año de 2 dígitos.
    """
    if not expte or not expte.strip():
        return ""
    s = expte.strip()
    m = re.fullmatch(r"0*(\d+)\s*/\s*(\d{2}|\d{4})", s)
    if not m:
        raise ValueError(
            f"expte '{expte}' inválido. Formato esperado: 'número/año' "
            f"(ej: '572/22' o '572/2022')."
        )
    num, anio = m.group(1), m.group(2)
    if len(anio) == 4:
        anio = anio[-2:]  # tomamos los últimos 2 dígitos
    return f"{num}/{anio}"


def validar_token_busqueda(s: str, *, nombre: str = "descriptores") -> str:
    """Valida que el texto de búsqueda tenga al menos 4 chars normalizados.

    El portal trata tokens de ≤3 chars como wildcard y devuelve el corpus
    completo. Mejor rechazar.
    """
    if not s or not s.strip():
        return ""
    s = s.strip()
    # Quitar tildes para el conteo (el portal las ignora)
    normalizado = "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )
    # Tokens separados por espacio: cada uno debe tener ≥4 chars salvo que
    # sean comillas o tokens compuestos.
    tokens = re.findall(r"\w+", normalizado)
    if not tokens:
        return ""
    cortos = [t for t in tokens if len(t) < 4]
    if cortos and not any(len(t) >= 4 for t in tokens):
        raise ValueError(
            f"{nombre} '{s}' tiene tokens demasiado cortos ({cortos}). "
            f"El portal trata tokens de ≤3 caracteres como wildcard y "
            f"devuelve el catálogo completo. Usá términos de ≥4 caracteres."
        )
    return s


def sanitizar_texto_query(s: str, *, nombre: str = "query") -> str:
    """Saca chars no-imprimibles y caracteres que el portal rechaza.

    - Newlines/tabs → espacio
    - Zero-width, format chars → quitados
    - `&` → 'y' (el portal trata `&` como inicio de QS y rompe)
    - `/` → espacio en queries de texto (mantener en expte)
    - Colapsar espacios
    """
    if not s:
        return ""
    out = []
    for c in s:
        cat = unicodedata.category(c)
        if cat in ("Cc", "Cf", "Cn"):  # control, format, unassigned
            out.append(" ")
        elif c == "&":
            out.append(" y ")
        elif c == "/":
            out.append(" ")
        else:
            out.append(c)
    cleaned = "".join(out)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned
