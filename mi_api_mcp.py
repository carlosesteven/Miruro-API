#!/usr/bin/env python3
"""MCP server (stdio) para MI-API. Mismo diseño que camaras_mcp.py / jkanime_relator_mcp.py.

Expone acciones para diagnosticar y forzar el refresco del cf_clearance de Cloudflare que
necesita el pipe de Miruro (ver SESSION_LOG.md — Miruro empezó a exigir resolver un challenge
de Cloudflare, y cf_refresher.py es el proceso que lo resuelve con un navegador real bajo Xvfb
y publica {cookie, headers} en Redis para que api.py los use).

cf_refresher.py ya corre solo por systemd timer y ya avisa por Telegram (vía `hermes send`,
notify_telegram() en el propio script) si no logra resolver el challenge — este MCP es la vía
manual: para chequear el estado sin entrar al servidor, o para forzar un refresco ahora mismo
sin esperar al timer.
"""
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

BASE_DIR = Path(__file__).resolve().parent
VENV_PYTHON = str(BASE_DIR / "venv" / "bin" / "python")
CF_REFRESHER = str(BASE_DIR / "cf_refresher.py")

import sys
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env")

import redis.asyncio as aioredis

REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None
# Cookie is per FALLBACK_TOPIC group now (each group isolated end to end, cookie included) —
# this MCP runs on the home node, so it checks THIS node's own group.
FALLBACK_TOPIC = os.getenv("FALLBACK_TOPIC", "default")
REDIS_KEY_CF_CLEARANCE = f"miruro_api:cf_clearance:{FALLBACK_TOPIC}"

mcp = FastMCP("mi-api")


@mcp.tool()
async def estado_cf_clearance() -> dict:
    """Chequea si hay un cf_clearance vigente para el pipe de Miruro y hace cuánto se
    actualizó. Si no hay ninguno (o está por vencer), el pipe (/episodes, /watch,
    /recent-episodes) va a estar devolviendo 403 hasta que se refresque."""
    r = aioredis.Redis(
        host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True
    )
    try:
        ttl = await r.ttl(REDIS_KEY_CF_CLEARANCE)
        raw = await r.get(REDIS_KEY_CF_CLEARANCE)
    finally:
        await r.aclose()

    if not raw:
        return {"vigente": False, "detalle": "No hay ningún cf_clearance guardado en Redis."}

    data = json.loads(raw)
    edad_seg = int(time.time()) - data.get("updated_at", 0)
    return {
        "vigente": True,
        "ttl_restante_seg": ttl,
        "actualizado_hace_seg": edad_seg,
        "fuente": data.get("source"),
    }


@mcp.tool()
def refrescar_cf_clearance() -> str:
    """Dispara AHORA el refresco del cf_clearance (navegador real vía Xvfb resolviendo el
    challenge de Cloudflare), sin esperar al timer programado. Corre en background: la
    respuesta es inmediata, el resultado real tarda ~10-45s. Si falla, cf_refresher.py ya
    manda su propia alerta por Telegram — no hace falta chequear el resultado acá, pero
    podés usar estado_cf_clearance() en un rato para confirmar que se actualizó."""
    subprocess.Popen(
        ["xvfb-run", "-a", VENV_PYTHON, CF_REFRESHER],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return "🔄 Refresco de cf_clearance disparado en background (~10-45s). Usá estado_cf_clearance() en un rato para confirmar."


if __name__ == "__main__":
    mcp.run()
