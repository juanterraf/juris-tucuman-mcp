#!/usr/bin/env bash
# Script para regenerar el bundle de deps y empacar el .mcpb
# Requiere:
#   - Python 3.10+ con pip
#   - npx (Node.js) para correr @anthropic-ai/mcpb
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Limpiando server/lib/ y .mcpb previos..."
rm -rf server/lib *.mcpb

echo "==> Instalando deps Python en server/lib/..."
python -m pip install --target server/lib --upgrade -r requirements.txt

echo "==> Empaquetando .mcpb..."
npx --yes @anthropic-ai/mcpb pack

echo ""
echo "✅ Listo. Archivo .mcpb generado en la raíz del proyecto."
ls -lh *.mcpb 2>/dev/null
