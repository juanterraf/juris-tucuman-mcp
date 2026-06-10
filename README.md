# Jurisprudencia PJ Tucumán — MCP Server

Servidor MCP (Model Context Protocol) que le permite a Claude consultar
fallos del Poder Judicial de Tucumán directamente desde la conversación.

Buscar jurisprudencia, descargar el texto completo de una sentencia, ver
las últimas resoluciones de un tribunal — todo sin salir del chat.

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

## 🛠️ Tools que expone

| Tool | Para qué sirve |
|------|----------------|
| `buscar_fallos` | Búsqueda combinable por descriptores, carátula, partes, número de sentencia/expediente, rango de fechas y tribunal. Devuelve metadatos + sumario de cada fallo. |
| `descargar_texto_fallo` | Trae el texto íntegro de un fallo dado su número de registro. |
| `listar_fallos_recientes` | Atajo para ver las últimas resoluciones, opcionalmente filtrando por tribunal. |

---

## 📦 Instalación

### Opción A — Claude Desktop con `.mcpb` (recomendada)

1. Bajá el `.mcpb` de la sección [Releases](https://github.com/juanterraf/juris-tucuman-mcp/releases) del repo.
2. Doble click en el archivo. Claude Desktop te pregunta si querés instalar la extensión.
3. Confirmá. Listo: las 3 tools quedan disponibles en cualquier conversación de Claude Desktop.

> **Nota de portabilidad:** los `.mcpb` precompilados que publicamos en
> Releases están bundleados con Python 3.14 para Windows x64. Si usás
> macOS, Linux u otra versión de Python, regeneralo localmente con
> `./scripts/build.sh` (ver "Build desde fuente" más abajo).

### Opción B — Manual con Python (cualquier plataforma)

Para Claude Desktop, Claude Code o cualquier cliente MCP que soporte stdio.

```bash
git clone https://github.com/juanterraf/juris-tucuman-mcp.git
cd juris-tucuman-mcp

python -m venv .venv
# Linux/Mac:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

Probá que arranque:

```bash
python server/main.py --transport stdio
# Debería quedarse esperando JSON-RPC en stdin. Ctrl-C para salir.
```

#### Configuración en Claude Desktop

Editá el archivo de config:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

Agregá:

```json
{
  "mcpServers": {
    "juris-tucuman": {
      "command": "/ruta/absoluta/al/repo/.venv/bin/python",
      "args": ["/ruta/absoluta/al/repo/server/main.py", "--transport", "stdio"]
    }
  }
}
```

(En Windows el `command` es `C:\\ruta\\al\\repo\\.venv\\Scripts\\python.exe` con backslashes escapados.)

Reiniciá Claude Desktop. Las tools aparecen en el ícono de "extensiones".

#### Configuración en Claude Code

```bash
claude mcp add --transport stdio juris-tucuman \
  /ruta/absoluta/al/repo/.venv/bin/python \
  /ruta/absoluta/al/repo/server/main.py --transport stdio
```

### Opción C — Servidor HTTP remoto

Para correr el MCP en un VPS y consumirlo desde Claude.ai como custom
integration, o desde múltiples clientes a la vez.

```bash
python server/main.py --transport streamable-http --host 127.0.0.1 --port 9000
```

Recomendaciones:
- Poné nginx adelante con HTTPS (Let's Encrypt) bajo un subdominio propio.
- Agregale autenticación (header `Authorization: Bearer ...`) antes de exponerlo público.

> **Atención:** si el VPS está en un rango de IPs que el portal del PJ
> Tucumán bloquea (típico de hosting/datacenter), el MCP no va a poder
> scrapear. Solucionate con un proxy residencial argentino (IPRoyal,
> Smartproxy, BrightData) y seteá la env var `SAEJUSBOT_PROXY=http://...`
> al arrancar el server.

---

## 💡 Ejemplos de uso

Una vez instalado, le decís a Claude cosas como:

> *"Buscame fallos sobre 'despido discriminatorio' en Cámara del Trabajo
> del último año y resumime los argumentos más comunes."*

> *"¿Qué jurisprudencia hay del TRIBUNAL DE IMPUGNACION sobre homicidio
> simple agravado por uso de arma?"*

> *"Traeme el texto completo del fallo registro 00077887 y citame
> textualmente la parte sobre dolo eventual."*

> *"Listame las últimas 10 sentencias publicadas, filtrando solo la
> Corte Suprema."*

Claude llama internamente a las tools del MCP y te devuelve la respuesta
con referencias verificables (número de registro, tribunal, fecha).

---

## 🔧 Build desde fuente

Si querés regenerar el `.mcpb` para tu plataforma (o porque modificaste
el código):

```bash
./scripts/build.sh
```

Eso hace:
1. Bundla las deps Python en `server/lib/` con `pip install --target`.
2. Empaqueta todo (manifest + código + lib) en un `.mcpb` con la CLI oficial
   de Anthropic (`npx @anthropic-ai/mcpb pack`).

Requisitos: Python 3.10+ y Node.js (para `npx`).

---

## 📂 Estructura

```
juris-tucuman-mcp/
├── manifest.json       # Spec MCPB v0.3
├── server/
│   ├── main.py         # FastMCP app + 3 tools
│   ├── scraper.py      # Scraper standalone del portal
│   └── lib/            # Deps Python (generadas por build.sh, ignoradas por git)
├── scripts/
│   └── build.sh        # Bundle deps + empaqueta .mcpb
├── requirements.txt
├── README.md
└── LICENSE             # MIT
```

---

## 🧪 Cómo funciona internamente

- **Scraper** (`scraper.py`): hace requests HTTP al portal con cookies de
  sesión PHP, parsea las páginas de resultados y de detalle con BeautifulSoup.
- **MCP server** (`main.py`): construido con [FastMCP](https://github.com/modelcontextprotocol/python-sdk)
  (SDK oficial Python). Cada tool valida sus argumentos con Pydantic.
- **Sin estado en disco**: cada request al MCP genera requests al portal.
  No hay caché local. La idea es que Claude consume el dato y lo procesa
  en el momento.
- **Sesión HTTP reutilizable**: el server mantiene una `requests.Session`
  entre llamadas para no perder cookies/`session_vistab` (necesario para
  que la página de detalle del fallo devuelva el texto completo).

---

## 🐛 Troubleshooting

### "No pude consultar el portal"
- Verificá que tu IP tenga acceso: `curl -sI https://juris.justucuman.gov.ar/`. Debería devolver `200`.
- Si devuelve `403`, el portal está bloqueando tu IP. Probá desde otra red
  o configurá un proxy residencial via `SAEJUSBOT_PROXY`.

### "No encuentro el fallo / texto vacío"
- El número de registro tiene formato `00077887` (8 dígitos) o `00077887-01`
  (con sufijo). Probá sin el sufijo.
- La primera vez que pedís un fallo en una sesión, el server hace una
  "búsqueda dummy" para inicializar la sesión PHP del portal. Si falla,
  reintentá.

### El `.mcpb` se rechaza al instalar
- Verificá la versión de Claude Desktop (`>=0.10.0`).
- Asegurate de tener Python en el PATH del sistema (algunos `.mcpb` lo requieren).

---

## 🤝 Contribuir

Issues y PRs bienvenidos. Algunas ideas pendientes:

- [ ] Bundle multi-plataforma (Windows / macOS / Linux) en GitHub Releases
- [ ] Tool `comparar_fallos` que tome 2+ registros y resalte diferencias
- [ ] Soporte para `uv` server type (sin bundling, deps al vuelo)
- [ ] Caché opcional en disco para evitar re-descargas

---

## 📄 Licencia

MIT — ver [LICENSE](LICENSE).
