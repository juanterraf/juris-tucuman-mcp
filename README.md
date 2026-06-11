# Jurisprudencia PJ Tucumán — MCP Server

Servidor MCP (Model Context Protocol) que le permite a Claude consultar
fallos del Poder Judicial de Tucumán directamente desde la conversación.

Buscar jurisprudencia con filtros precisos, descargar el texto completo
de una sentencia, ver las últimas resoluciones de un tribunal — todo sin
salir del chat.

> **⚠️ Disclaimer**
>
> Este proyecto consulta el portal público
> [juris.justucuman.gov.ar](https://juris.justucuman.gov.ar) con volumen
> equivalente a un usuario humano navegando. Las consultas las dispara
> Claude en respuesta a pedidos explícitos del usuario. No es scraping
> masivo ni redistribución de datos.
>
> La información obtenida **NO constituye asesoramiento jurídico**.
> Verificar siempre contra la fuente oficial antes de usar profesionalmente.
>
> Si formás parte del PJ Tucumán y querés que este proyecto se baje, abrí
> un issue.

---

## 🆕 v1.0 — Cambios clave vs v0.1.x

- **Filtro por tribunal emisor** (no por "tribunal de origen de casación"). 78 cámaras y salas con códigos individuales (`Cámara del Trabajo Sala 3`, `Tribunal de Impugnación Capital`, etc.). Antes: filtrar por "Cámara del Trabajo" devolvía solo fallos de CSJ.
- **Carátulas completas** con actor y demandado (antes salía solo "S/ DESPIDO"). Fix: se sacó el GET de warmup que degradaba la sesión.
- **Nueva tool `obtener_fallo_por_registro`**: trae metadata + texto íntegro en una sola llamada, sin búsqueda previa. Usa `/mostrar_fallo.php` (más liviano, sin `session_vistab`).
- **Validación robusta**: fechas en múltiples formatos, autocompletado si falta una, error claro ante tokens de búsqueda ≤3 chars que el portal trata como wildcard.
- **`solo_doctrina_legal`**: nuevo flag para restringir a precedentes vinculantes de la CSJT (~5500 fallos).
- **`total_disponible_en_portal`** en cada response: Claude ahora sabe si hay más fallos de los que trajo.
- **Texto truncable**: fallos muy largos (>200k chars) se paginan con `max_chars` + `offset`.
- **Encoding utf-8 forzado**: se acabaron los `Ã±` y `Ã©`.
- **Retries con backoff** para 5xx y timeouts transitorios.

---

## 🛠️ Tools que expone

| Tool | Para qué sirve |
|------|----------------|
| `buscar_fallos` | Búsqueda combinable: descriptores (AND implícito), carátula (objeto/materia), partes, número de sentencia/expte, rango de fechas, tribunal emisor, solo doctrina legal. |
| `obtener_fallo_por_registro` | Trae un fallo completo (metadata + texto íntegro) dado su número, sin búsqueda previa. |
| `descargar_texto_fallo` | Compat v0.1.x: solo el texto íntegro. Mismo backend que `obtener_fallo_por_registro`. |
| `listar_fallos_recientes` | Atajo para últimas resoluciones, opcionalmente filtrando por tribunal. |
| `listar_tribunales` | Catálogo canónico: 78 emisores con sub-salas + 28 de origen de casación. |

---

## 📦 Instalación

### Opción A — Claude Desktop con `.mcpb` (recomendada)

1. Bajá el `.mcpb` de la sección [Releases](https://github.com/juanterraf/juris-tucuman-mcp/releases) del repo.
2. Doble click en el archivo. Claude Desktop te pregunta si querés instalar la extensión.
3. Confirmá. Listo: las 5 tools quedan disponibles en cualquier conversación de Claude Desktop.

> **Nota de portabilidad:** los `.mcpb` precompilados que publicamos en
> Releases están bundleados con Python 3.14 para Windows x64. Si usás
> macOS, Linux u otra versión de Python, regeneralo localmente con
> `./scripts/build.sh` (ver "Build desde fuente" más abajo).

### Opción B — Manual con Python (cualquier plataforma)

```bash
git clone https://github.com/juanterraf/juris-tucuman-mcp.git
cd juris-tucuman-mcp

python -m venv .venv
# Linux/Mac:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1

pip install -r requirements.txt

# Verificá que arranque:
python server/main.py --transport stdio
# Ctrl-C para salir.
```

**Claude Desktop config** (`~/Library/Application Support/Claude/claude_desktop_config.json` en macOS, `%APPDATA%\Claude\claude_desktop_config.json` en Windows, `~/.config/Claude/claude_desktop_config.json` en Linux):

```json
{
  "mcpServers": {
    "juris-tucuman": {
      "command": "/ruta/al/repo/.venv/bin/python",
      "args": ["/ruta/al/repo/server/main.py", "--transport", "stdio"]
    }
  }
}
```

**Claude Code**:

```bash
claude mcp add --transport stdio juris-tucuman \
  /ruta/al/repo/.venv/bin/python \
  /ruta/al/repo/server/main.py --transport stdio
```

### Opción C — Servidor HTTP remoto

Para correr el MCP en un VPS y consumirlo desde Claude.ai como custom
integration:

```bash
python server/main.py --transport streamable-http --host 127.0.0.1 --port 9000
```

> **Atención:** si el VPS está en un rango de IPs que el portal del PJ
> Tucumán bloquea (típico de hosting/datacenter), el MCP no va a poder
> scrapear. Solucionate con un proxy residencial argentino y seteá la
> env var `SAEJUSBOT_PROXY=http://...` al arrancar.

---

## 💡 Ejemplos de uso

Una vez instalado, le decís a Claude cosas como:

> *"Buscame fallos sobre 'despido discriminatorio' en Cámara del Trabajo Sala 3 de los últimos 6 meses y resumime los argumentos comunes."*

> *"Necesito jurisprudencia consolidada (doctrina legal) sobre prescripción adquisitiva."*

> *"Listame las últimas 30 sentencias del Tribunal de Impugnación Capital."*

> *"Bajame el texto completo del fallo registro 00078941 y resaltame las citas a la CSJN."*

> *"Buscame fallos donde la actora sea Pérez y la demandada sea Municipalidad."*

> *"Comparame los argumentos de 3 fallos sobre dolo eventual."*

Claude llama internamente a las tools del MCP y te devuelve la respuesta
con referencias verificables (número de registro, tribunal, fecha).

---

## 🧠 Caveats importantes del portal

Documentados para que entiendas si una búsqueda devuelve lo que esperabas:

### Sobre las búsquedas de texto
- **Tokens ≤3 caracteres se RECHAZAN** del lado server. El portal los trata como wildcard y devuelve el catálogo completo (120k+ fallos). Usá ≥4 chars.
- **AND implícito**: `"despido discriminatorio"` (sin comillas en el JSON, espacio en el medio) = fallos con AMBAS palabras. Para frase exacta, usá comillas: `'"despido discriminatorio"'`.
- **OR/AND explícitos NO funcionan** — el portal los trata literal.
- **Tildes y ñ se ignoran** en el portal: `prescripción` matchea `prescripcion`.

### Sobre fechas
- El portal solo aplica el filtro si **AMBAS** `fecha_desde` y `fecha_hasta` están presentes. El server **autocompleta el extremo faltante** para que igual funcione (`fecha_hasta` sin `fecha_desde` → desde `01/01/1990`).
- Si el formato es inválido (ej. `'2025-01-01'`, `'1/1/25'`), el server lo **normaliza a `dd/mm/yyyy`** automáticamente.
- Rangos invertidos (`fecha_desde > fecha_hasta`) se rechazan con error.

### Sobre el filtro `tribunal`
- Acepta cualquier nombre con tildes/case mixto: `"Cámara del Trabajo Sala 3"`, `"tribunal de impugnacion capital"`, `"CSJ"`.
- Si hay ambigüedad (ej. `"Tribunal de Impugnación"` solo, sin "Capital" o "Concepción"), la tool devuelve `candidatos` y Claude debería preguntar al usuario.
- Llamá a `listar_tribunales` si querés ver todas las opciones.

### Sobre `solo_doctrina_legal`
- Restringe a precedentes vinculantes marcados por la CSJT (~5500 fallos, ~5% del corpus).
- Útil cuando el usuario pregunta por *jurisprudencia consolidada/vinculante*.

### Sobre número de sentencia
- **No es único**: se reinicia por tribunal y año. `sentencia=803` puede devolver decenas de fallos distintos.
- Combiná con `tribunal` y/o rango de fechas para localizar uno específico.

### Sobre número de expediente
- Formato `'número/aa'` (año 2 dígitos). El server acepta `'572/2022'` y normaliza.

---

## 🔧 Build desde fuente

Si querés regenerar el `.mcpb` para tu plataforma:

```bash
./scripts/build.sh
```

Eso:
1. Bundla las deps Python en `server/lib/` con `pip install --target`.
2. Empaqueta todo en un `.mcpb` con la CLI oficial (`npx @anthropic-ai/mcpb pack`).

Requisitos: Python 3.10+ y Node.js.

---

## 📂 Estructura

```
juris-tucuman-mcp/
├── manifest.json       # Spec MCPB v0.3
├── server/
│   ├── main.py         # FastMCP app + 5 tools
│   ├── scraper.py      # Scraper standalone del portal
│   ├── validators.py   # Validadores de fechas, registros, expedientes, etc.
│   └── lib/            # Deps Python (generadas por build.sh)
├── scripts/
│   └── build.sh        # Bundle deps + empaqueta .mcpb
├── requirements.txt
├── README.md
└── LICENSE             # MIT
```

---

## 🧪 Cómo funciona internamente

- **Scraper** (`scraper.py`): requests HTTP con cookies de sesión PHP,
  parsea las páginas de resultados y de detalle con BeautifulSoup.
  Sesiones con retries automáticos (urllib3.Retry, backoff 1.5x para
  5xx y timeouts).
- **Validadores** (`validators.py`): chequean y normalizan inputs antes
  de mandarlos al portal. Evitan que el portal devuelva el corpus
  completo silenciosamente cuando un input es inválido.
- **MCP server** (`main.py`): construido con [FastMCP](https://github.com/modelcontextprotocol/python-sdk)
  (SDK oficial Python). Cada tool valida sus argumentos con Pydantic.
  Sesión HTTP compartida entre llamadas, con `threading.Lock` y TTL de
  10 minutos para reciclar `PHPSESSID` expirados.
- **Sin estado en disco**: cada request al MCP genera requests al portal.
  No hay caché local.

---

## 🐛 Troubleshooting

### "No pude consultar el portal"
- Verificá que tu IP tenga acceso: `curl -sI https://juris.justucuman.gov.ar/`. Debería devolver `200`.
- Si devuelve `403`, el portal está bloqueando tu IP (típico de datacenters). Probá desde otra red o configurá un proxy residencial con la env `SAEJUSBOT_PROXY`.

### "No reconozco el tribunal X"
- Llamá a `listar_tribunales`. Te va a mostrar los 78 nombres canónicos.
- Probá quitando palabras: `"Cámara del Trabajo Sala 3"` mejor que `"Camara Trabajo S3"`.

### "tokens demasiado cortos"
- El portal trata tokens ≤3 chars como wildcard. Usá ≥4 chars en `descriptores`.

### El `.mcpb` se rechaza al instalar
- Verificá la versión de Claude Desktop (>=0.10.0).
- Asegurate de tener Python en el PATH del sistema.

---

## 🤝 Contribuir

Issues y PRs bienvenidos. Ideas pendientes (ver `fuera_de_alcance_v1` en el plan v1):

- [ ] Búsqueda por juez emisor (parsear firmas digitales)
- [ ] Tool `comparar_fallos` que destaca diferencias argumentales
- [ ] Búsqueda por normativa citada (artículo de ley)
- [ ] Multi-tribunal en una request (fan-out)
- [ ] Bundle multi-plataforma en GitHub Releases

---

## 📄 Licencia

MIT — ver [LICENSE](LICENSE).
