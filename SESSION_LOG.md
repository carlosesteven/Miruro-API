# Session Log

Historial de cambios realizados por Claude Code en este proyecto.

---

## Sesión 2026-05-22

### CLAUDE.md — Inicialización
- Se creó el archivo `CLAUDE.md` con documentación del proyecto para futuras sesiones: comandos de ejecución, arquitectura general, flujo de seguridad, encoding de IDs y variables de entorno.

### Redis Cache en `/recent-episodes`
**Archivos modificados:** `api.py`, `requirements.txt`, `.env`

**Qué se implementó:**
- Cache en Redis para el endpoint `GET /recent-episodes` con TTL de 2 horas (configurable).
- Mientras el cache esté vigente, no se ejecuta ningún request al Miruro pipe (`_fetch_raw_recents()`).
- Si Redis no está disponible, el endpoint sigue funcionando sin cache (fallos silenciosos con `try/except`).

**Variables de entorno agregadas a `.env`:**
| Variable | Valor por defecto | Propósito |
|---|---|---|
| `REDIS_HOST` | `localhost` | Host del servidor Redis |
| `REDIS_PORT` | `6379` | Puerto Redis |
| `REDIS_PASSWORD` | — | Contraseña Redis |
| `CACHE_RECENT_EPISODES_HOURS` | `2` | Duración del cache en horas (cambiar aquí para ajustar el TTL) |

**Key en Redis:**
```
miruro_api:cache:recent_episodes
```

Comandos para inspeccionar el cache:
```bash
# Ver si existe y su contenido
redis-cli -h <HOST> -p <PORT> -a <PASSWORD> GET miruro_api:cache:recent_episodes

# Ver segundos restantes de vida
redis-cli -h <HOST> -p <PORT> -a <PASSWORD> TTL miruro_api:cache:recent_episodes

# Invalidar manualmente (fuerza refetch en el próximo request)
redis-cli -h <HOST> -p <PORT> -a <PASSWORD> DEL miruro_api:cache:recent_episodes
```

**Dependencia agregada a `requirements.txt`:** `redis` (v4+ incluye asyncio de forma nativa; no se necesita `redis[asyncio]`).

**Estructura del código en `api.py`:**
```python
# Constantes (al inicio del archivo, junto a la configuración)
CACHE_RECENT_EPISODES_HOURS = int(os.getenv("CACHE_RECENT_EPISODES_HOURS", "2"))
CACHE_RECENT_EPISODES_TTL   = CACHE_RECENT_EPISODES_HOURS * 3600
REDIS_KEY_RECENT_EPISODES   = "miruro_api:cache:recent_episodes"

redis_client = aioredis.Redis(host=..., port=..., password=..., decode_responses=True)
```

---

## Sesión 2026-07-03

### Servicio systemd para auto-inicio en boot
**Archivos modificados:** `README.md` (nuevo item "Deploy (production — systemd service, auto-start on boot)")
**Archivo creado (fuera del repo):** `/etc/systemd/system/mi-api.service`

**Qué se implementó:**
- Unidad systemd (`mi-api.service`) que reemplaza el arranque manual con `nohup ... &` tras un reinicio de la máquina. Activa el venv (`source venv/bin/activate`) y ejecuta `python -m uvicorn api:app --host 0.0.0.0 --port 8848` con `exec`, redirigiendo salida a un log timestamped (`uvicorn-$(date +%F-%H%M%S).log`) dentro del repo, igual convención que el flujo manual del README.
- `Type=simple` + `exec` en vez de `nohup`/`&`: bajo systemd no hacen falta (systemd ya desacopla el proceso de la terminal) y romperían el tracking del PID si se usaran.
- `Restart=on-failure` + `RestartSec=5`: reinicio automático ante crash.
- `WantedBy=multi-user.target` + `systemctl enable`: arranque automático en cada boot.

**Incidente al activar:** al iniciar el servicio por primera vez, hubo ~9 reinicios en bucle por `Errno 98: address already in use` — un proceso `nohup` manual previo seguía ocupando el puerto 8848. Se resolvió al matar ese proceso; el servicio quedó estable con un solo proceso corriendo bajo systemd. Este caso ya quedó documentado como advertencia en el README.

**Comandos clave:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable mi-api.service
sudo systemctl start mi-api.service
sudo systemctl status mi-api.service
journalctl -u mi-api -f
```

---

## Sesión 2026-07-06

### [TEMPORAL] Forzar `type: mp4` → `type: hls` solo en `/watch/{provider}/{anilist_id}/{category}/{slug}`
**Archivo modificado:** `api.py`
**Estado:** ACTIVO. Pensado para eliminarse en el futuro — instrucciones exactas de reversión más abajo.

**Motivo:** ajuste puntual solicitado por el usuario para el caso `/watch/ally/178789/sub/allmanga-2` (y cualquier otra ruta `/watch/...`): algunos streams del pipe llegan con `"type": "mp4"` y se necesita que temporalmente se reporten como `"type": "hls"`. No se toca ningún otro valor de `type` (p. ej. `"embed"` se deja intacto).

**Qué se implementó:**
- Se agregó la función `_force_mp4_to_hls_temp(data)` en `api.py`, justo antes de `get_watch_sources` (línea ~916, antes del decorador `@app.get("/watch/{provider}/{anilist_id}/{category}/{slug}")`). Recorre `data["streams"]` y donde `stream["type"] == "mp4"` lo reemplaza por `"hls"`.
- Se modificó el `return` final de `get_watch_sources` (antes: `return await get_sources(...)`) para que capture el resultado en `result` y le aplique `_force_mp4_to_hls_temp(result)` antes de devolverlo.
- **Alcance:** el cambio vive únicamente dentro de `get_watch_sources`. El endpoint `/sources` (llamado directo, sin pasar por el slug de `/watch/...`) NO se ve afectado — se verificó en vivo que sigue devolviendo los streams sin modificar.

**Diff aplicado (para referencia exacta):**
```python
# ANTES (dentro de get_watch_sources, última línea de la función):
    return await get_sources(episodeId=target_id, provider=provider, anilistId=anilist_id, category=category)

# DESPUÉS:
def _force_mp4_to_hls_temp(data: dict) -> dict:
    """TEMP (ver SESSION_LOG.md): fuerza streams con type=mp4 a hls. Borrar esta función
    completa y su única llamada en get_watch_sources para revertir. No toca "embed" ni "hls"."""
    for stream in data.get("streams", []):
        if stream.get("type") == "mp4":
            stream["type"] = "hls"
    return data

@app.get("/watch/{provider}/{anilist_id}/{category}/{slug}")
async def get_watch_sources(...):
    ...
    result = await get_sources(episodeId=target_id, provider=provider, anilistId=anilist_id, category=category)
    return _force_mp4_to_hls_temp(result)  # TEMP: quitar esta línea para revertir (ver SESSION_LOG.md)
```

**Cómo revertir (cuando el usuario lo pida, ej. "elimina el ajuste temporal de mp4 a hls"):**
1. En `api.py`, borrar la función completa `_force_mp4_to_hls_temp` (las 6 líneas, desde `def _force_mp4_to_hls_temp(data: dict) -> dict:` hasta su `return data`).
2. En `get_watch_sources`, reemplazar las dos líneas:
   ```python
   result = await get_sources(episodeId=target_id, provider=provider, anilistId=anilist_id, category=category)
   return _force_mp4_to_hls_temp(result)  # TEMP: quitar esta línea para revertir (ver SESSION_LOG.md)
   ```
   por la línea original:
   ```python
   return await get_sources(episodeId=target_id, provider=provider, anilistId=anilist_id, category=category)
   ```
3. Borrar esta sección del `SESSION_LOG.md` (o marcarla como "revertido" con fecha).
4. No hay variables de entorno, dependencias ni otros archivos involucrados — es autocontenido en `api.py`.

**Verificación tras revertir:** `curl` a `/watch/ally/178789/sub/allmanga-2` con el `x-api-key` correcto debe volver a mostrar `"type": "mp4"` en los streams que originalmente lo traían así (ya no todos como `"hls"`).

---

## Sesión 2026-08-15

### Diagnóstico: `/watch/kiwi/209983/sub/animepahe-6` falla/tarda ~60s
**Investigación, sin cambios de código en esta parte.**

- Se descartó colisión de slugs: los IDs de episodios de `kiwi/sub` para el anime 209983 son únicos y bien formados (`animepahe:6772:77466:1` para el episodio 6).
- Se aisló el problema llamando directamente a `get_sources()`: **todo** episodio del provider `kiwi` (backend `animepahe`) falla con `503 Pipe unavailable` tras ~60s (dos intentos de `_pipe_get` de ~30s cada uno, sin timeout explícito configurado). Otros providers del mismo anime (`ally`, `pewe`) resuelven bien en <0.5s.
- Conclusión: no es un bug de `api.py` — la integración de Miruro con `animepahe` está caída/colgándose del lado de ellos. El código no tiene timeout explícito en `pipe_session.get()`, por lo que en vez de fallar rápido, el usuario espera ~60s.
- Pendiente (no implementado): agregar `timeout=` explícito a las llamadas del pipe en `_pipe_get` para que este tipo de fallas retornen en segundos, no en un minuto.

### Feature: `BLOCKED_EPISODE_PREFIXES` — ocultar providers rotos de `/episodes`
**Archivos modificados:** `api.py`, `.env`, `CLAUDE.md`

**Qué se implementó (no es un ajuste temporal, es una feature permanente):**
- Nueva env var `BLOCKED_EPISODE_PREFIXES` (comma-separated), parseada como set en `api.py` (junto a la config de cache, ~línea 42).
- Nueva función `_filter_blocked_prefixes(data)` (justo antes de `_inject_source_slugs`): recorre `data["providers"][*]["episodes"][*]` y elimina cualquier episodio cuyo `id.split(":")[0]` esté en `BLOCKED_EPISODE_PREFIXES`. No-op si la env var está vacía.
- Se llama dentro de `get_episodes()` (`GET /episodes/{anilist_id}`), justo después de `_fetch_raw_episodes` y antes de `_inject_source_slugs` (tiene que ir antes porque `_inject_source_slugs` reescribe `ep["id"]` al formato slug `watch/...`, perdiendo el prefijo original).
- **Nota:** el provider (ej. `kiwi`) sigue apareciendo en la respuesta, pero con la lista de episodios vacía si todos sus IDs matchean el prefijo bloqueado. No se oculta el provider completo, solo los episodios cuyo prefijo esté bloqueado — así si un provider mezcla varios backends, solo se filtra el roto.

**Cómo usarlo (agregar más providers rotos en el futuro):**
```bash
# En .env, separar por coma:
BLOCKED_EPISODE_PREFIXES=animepahe,otroprovider
```
No requiere tocar código ni reiniciar nada más que el proceso (para recargar el `.env`).

**Estado actual:** `BLOCKED_EPISODE_PREFIXES=animepahe` activo en `.env`, por el problema de arriba. Quitar `animepahe` de esa lista (o vaciar la variable) cuando Miruro arregle su integración con animepahe — verificado en local que con la variable seteada, `kiwi/sub` en `/episodes/209983` devuelve 0 episodios y el resto de providers no se ven afectados.

**Nota:** este cambio NO toca `/watch/{provider}/.../{slug}` — si alguien pega directamente una URL vieja `/watch/kiwi/.../animepahe-X`, ese endpoint sigue intentando resolverla y tardará los ~60s de antes. Solo se filtra en el listado de `/episodes`.

### Fix: `timeUntilAiring` desactualizado en `/recent-episodes`
**Archivo modificado:** `api.py`

**Motivo:** la app Android (Kotlin) del usuario consume `/recent-episodes` y filtra episodios "recién salidos" comparando el campo `timeUntilAiring` contra un umbral muy chico (`api_miruro_recents_timestamp` en Remote Config, default 50 segundos). Se detectó que el `timeUntilAiring` que devuelve el pipe de Miruro (`path: "schedule"`) viene de un snapshot que Miruro cachea de su lado y no recalcula seguido — se verificó pidiendo `/recent-episodes` dos veces separadas por horas reales y la respuesta fue **byte-idéntica** (mismo MD5), con `timeUntilAiring` de un episodio que ya había salido hace horas todavía en positivo. Con un umbral de 50s, el filtro de la app casi nunca se cumplía hasta que el cache de Miruro se refrescaba (aparentemente 1 vez al día), por eso los episodios "recién salidos" solo aparecían casi al final del día.

**Qué se implementó:**
- Se duplicó el endpoint original tal cual estaba en `/recent-episodes-old` (función `get_recent_episodes_old`), sin ningún cambio — referencia/rollback rápido si hiciera falta comparar comportamiento.
- Se agregó `_recompute_time_until_airing(data)` (api.py, antes de `get_recent_episodes`): recorre la lista top-level de `/recent-episodes` y sobreescribe `item["timeUntilAiring"] = item["airingAt"] - int(time.time())` para cada item. `airingAt` sí es estable (viene de AniList, no se mueve), así que el recálculo queda exacto al segundo sin depender de qué tan viejo esté el snapshot de Miruro.
- Se llama **al servir la respuesta**, tanto si viene de cache Redis como si es fetch fresco — nunca se guarda el valor recalculado en cache, solo se cachea la data cruda (que sí puede quedarse cacheada 2h sin problema, ya que `airingAt`/título/imagen no cambian seguido).
- Se agregó `import time` a los imports de `api.py`.
- **No cambia el shape del JSON** (mismo campo, mismo nombre, mismo tipo int, puede quedar negativo si ya pasó — la app Kotlin lee `getInt("timeUntilAiring")` y compara con `<=`, funciona igual sin ningún cambio de la app). Verificado en local: `/recent-episodes-old` sigue devolviendo el valor viejo congelado (positivo), `/recent-episodes` devuelve el valor correcto (negativo, ~-20000s para un episodio que ya salió hace ~5.7h) — se comparó campo por campo que ningún otro dato cambió entre ambos endpoints, solo `timeUntilAiring`.

**Pendiente / no incluido en este cambio:** no se tocó `media.nextAiringEpisode.timeUntilAiring` (campo anidado) — la app Kotlin no lo usa para el filtro, solo lee `nextAiringEpisode.episode` para el número de episodio, así que no hacía falta.

### Filtro server-side: solo formato `TV` en `/recent-episodes`
**Archivo modificado:** `api.py`

**Motivo:** el único consumidor de `/recent-episodes` es la app Android, que ya descarta client-side todo lo que no sea `format == "TV"` (ver Kotlin del usuario). Confirmado con el usuario que no hay otro cliente (web, etc.) usando este endpoint que necesite los demás formatos. De 124 items que trae Miruro, solo 72 son `TV` — el resto (`ONA`, `TV_SHORT`, `MOVIE`, `SPECIAL`, `MUSIC`) se descartaban igual del lado del cliente, era payload/parseo desperdiciado.

**Qué se implementó:**
- Se agregó `_filter_tv_format(data)` (api.py, antes de `get_recent_episodes`): list comprehension que se queda solo con items donde `media.format == "TV"`.
- Se aplica dentro de `get_recent_episodes()`, después de leer de cache o de fetch fresco, antes de `_recompute_time_until_airing`. **No se filtra en `/recent-episodes-old`** — ese endpoint sigue devolviendo los 124 items con todos los formatos, intacto, como copia de referencia.
- No afecta el cache en Redis: se sigue cacheando la data cruda completa (los 124 items sin filtrar), el filtro se aplica solo al servir la respuesta — así si en el futuro se necesita otro formato desde otro endpoint/consumidor, la data completa sigue disponible sin re-pedirle a Miruro.

**Verificado en local:** `/recent-episodes` → 72 items, todos `format: "TV"`. `/recent-episodes-old` → 124 items, formatos mixtos (`TV`, `ONA`, `TV_SHORT`, `MOVIE`, `SPECIAL`, `MUSIC`), sin cambios.

### Fix: la app siempre mostraba "último episodio - 1"
**Archivo modificado:** `api.py`

**Motivo:** el usuario reportó que su app Android siempre muestra el episodio anterior al real. Su Kotlin calcula el episodio a mostrar así:
```kotlin
episodeAux = episode.getJSONObject("nextAiringEpisode").getInt("episode")
if (episodeAux > 1) episodeAux -= 1
```
Asume que `media.nextAiringEpisode.episode` siempre apunta al episodio siguiente al que ya salió (de ahí el `-1`). Se verificó sobre los 72 items `TV` de `/recent-episodes-old` que esto **no siempre es cierto**: 37/72 sí traían `nextAiringEpisode.episode == episode + 1` (la resta da bien), pero **34/72 traían `nextAiringEpisode.episode == episode`** (mismo valor, sin avanzar) — ahí la resta deja a la app mostrando un episodio menos del real. 1 item no tenía `nextAiringEpisode` (show `FINISHED`), ese caso ya lo maneja bien el Kotlin sin restar nada.

Es la misma raíz que el bug de `timeUntilAiring`: el snapshot que cachea Miruro no avanza `nextAiringEpisode` en sincronía con la hora real de emisión — para shows cuyo episodio salió después de que Miruro tomó su snapshot, ese campo se queda pegado en el mismo número que `episode` en vez de avanzar al siguiente. Ejemplo verificado: BLACK TORCH ep7, `nextAiringEpisode.episode` venía en `7` (igual, no `8`) — la app mostraba episodio 6.

**Qué se implementó:**
- Se agregó `_fix_next_airing_episode(data)` (api.py, antes de `get_recent_episodes`): para cada item, fuerza `media.nextAiringEpisode.episode = item["episode"] + 1`, sin importar el valor que traiga Miruro. No toca items sin `nextAiringEpisode` (los deja como vienen).
- Se llama al final de la cadena en `get_recent_episodes()` (después de `_filter_tv_format` y `_recompute_time_until_airing`), tanto en el camino de cache-hit como en el de fetch fresco. `/recent-episodes-old` no se toca.

**Verificado en local:** de los 72 items `TV`, los 72 quedan con `nextAiringEpisode.episode == episode + 1` (0 incorrectos). BLACK TORCH: `episode: 7`, `nextAiringEpisode.episode: 8` → la app calcularía `8 - 1 = 7`, correcto.

---

## Sesión 2026-09-07

### Diagnóstico: el usuario reporta "el API ha dejado de responder"
**Solo investigación, sin cambios de código.**

- El proceso uvicorn (systemd, pid 1282, puerto 8848) está corriendo normal. `GET /health` responde `200 {"status":"ok"}` en <5ms — el proceso en sí **no está caído**.
- Todo endpoint que depende del pipe de Miruro (`/episodes/{id}`, `/recent-episodes`, `/watch/{provider}/...`) devuelve **403 Forbidden `{"detail":"Pipe request failed"}`**. Confirmado en vivo con `curl` directo a `localhost:8848` y también reproducido llamando al pipe de Miruro directamente con `curl_cffi` (mismo protocolo que usa `_pipe_get` en `api.py`).
- La respuesta cruda del pipe (y también de `https://www.miruro.to/` a secas, sin ninguna ruta de API) es una página Cloudflare "Just a moment..." con el header `cf-mitigated: challenge` — es decir, Cloudflare está presentando un challenge JS/Turnstile interactivo a las requests que salen desde este servidor, y como no hay navegador real que lo resuelva, cualquier request de `curl_cffi` recibe 403 sin importar el `impersonate` usado (se probó con `chrome110` hasta `chrome146`, los 9 perfiles fallan idéntico — descarta que sea un desajuste de fingerprint TLS vs User-Agent).
- Revisando el log `uvicorn-2026-09-07-002027.log`: las requests al pipe funcionaban bien (200 OK) durante buena parte del día, empezaron a mezclarse con 403 esporádicos a partir de la línea ~5431, y escalaron a **100% de fallos** en las últimas ~100 requests antes de este diagnóstico. Patrón típico de Cloudflare subiendo el bot-score de la IP de salida del servidor hasta bloquearla del todo, no de un cambio o bug en `api.py`.

**Conclusión:** no es un bug del código ni de la config del proyecto — es Miruro/Cloudflare bloqueando la IP de salida de este servidor. `PIPE_EXTRA_HEADERS` no tiene ninguna cookie `cf_clearance` seteada actualmente (solo headers `sec-ch-*`/`accept-*`), así que no hay nada que revocar ahí.

**Pendiente / opciones no implementadas (requieren decisión del usuario):**
1. Esperar — el bot-score de Cloudflare a veces baja solo pasado un tiempo sin tráfico sospechoso.
2. Conseguir una cookie `cf_clearance` válida (resolviendo el challenge manualmente desde un navegador con la misma IP del servidor) y pasarla vía `PIPE_EXTRA_HEADERS` como `Cookie` — es frágil, expira y hay que repetirlo cada vez que Cloudflare vuelva a bloquear.
3. Cambiar la IP de salida del servidor (nueva IP del VPS, o proxy/relay) si el bloqueo resulta ser por reputación de esa IP específica.
4. Si el patrón se repite seguido, considerar bajar la frecuencia/volumen de requests al pipe (podría ser un bloqueo por rate más que por fingerprint).

**Actualización (misma sesión): se implementó la opción 2, automatizada.** El usuario consiguió un `cf_clearance` válido a mano desde su propio navegador (Chrome real, misma IP del servidor por acuerdo de red) y confirmamos que **no está atado a la IP de origen** — solo a que los headers `sec-ch-ua`/`user-agent`/etc. coincidan exactamente con los que Cloudflare vio al emitirla. A partir de eso se armó todo un pipeline para que esto no dependa de pegar cookies a mano:

### `api.py` — consumir cf_clearance desde Redis
- Nueva constante `REDIS_KEY_CF_CLEARANCE = "miruro_api:cf_clearance"`.
- Nueva función `_get_pipe_headers()` (antes de `_pipe_get`): lee un blob `{cookie, headers}` de Redis (cacheado en memoria 15s vía `CF_CLEARANCE_LOCAL_CACHE_SECONDS` para no pegarle a Redis en cada request) y arma los headers finales. **Importante:** si hay blob, se usan SUS headers tal cual (no se mezclan con el `HEADERS` estático) — mezclar `HEADERS["User-Agent"]` (mayúscula) con `blob["headers"]["user-agent"]` (minúscula) los deja como dos claves de dict distintas y manda dos headers `User-Agent` en la misma request, lo cual re-dispara el challenge. Si no hay blob en Redis, cae al comportamiento viejo (`HEADERS` estático, sin cookie).
- `_pipe_get` ahora llama `await _get_pipe_headers()` en vez de usar `HEADERS` directo, tanto en el intento inicial como en el retry.

### `cf_refresher.py` (nuevo, raíz del repo)
Resuelve el challenge de Cloudflare con un navegador real y publica la cookie en Redis para que `api.py` la use. Hallazgos clave del desarrollo:
- **Headless (Playwright normal, y también `patchright` que es un fork stealth) se queda colgado para siempre en "Just a moment..."** — Cloudflare lo detecta igual.
- **No-headless (headless=False) bajo Xvfb sí lo resuelve**, típicamente en 5-10s. `patchright` (no Playwright estándar) fue el que finalmente funcionó de forma confiable en modo headful.
- Los headers que hay que guardar junto a la cookie tienen que salir de un **fetch() same-origin real** disparado después de que el challenge se resuelve (no de la navegación inicial, que trae `sec-fetch-dest: document` en vez del shape que usa `api.py`) — hubo que esperar a que la navegación post-challenge se asiente (`wait_for_load_state("networkidle")`) antes de disparar ese fetch, si no el `page.evaluate()` explota con "Execution context was destroyed" por la carrera con la redirección de Cloudflare.
- Publica `{cookie, headers, updated_at, source}` en Redis (`miruro_api:cf_clearance`), TTL de 25 min como red de seguridad (si el script deja de correr, `api.py` cae solo al comportamiento sin cookie en vez de reintentar una cookie ya vencida para siempre).
- **Modo de operación (decisión del usuario):** NO refresca incondicionalmente en cada corrida del timer. Antes de lanzar el navegador chequea el TTL restante en Redis (`_current_ttl()`); si todavía quedan más de `MIN_TTL_BEFORE_REFRESH_SECONDS` (10 min), no hace nada (`SKIP`, ~1s). Solo lanza Chromium de verdad cuando el margen es bajo. Verificado en vivo: con TTL alto → `SKIP` en 1.1s; forzando TTL a 300s → refresca de verdad en ~13s.
- **Alerta por Telegram en caso de fallo:** función `notify_telegram()` que hace `subprocess.run([HERMES_BIN, "send", "--to", "telegram", "-q", mensaje])` — mismo patrón que `wordpress_blog_animecast/animecast_ingest/notify.py` (reusa el bot de Telegram YA configurado en Hermes, `~/.hermes/.env` + `config.yaml`, sin tocar tokens desde MI-API). Con debounce de 1h (`miruro_api:cf_refresher:last_alert` en Redis, `SET NX EX`) para no spamear si Cloudflare escala a un challenge interactivo por un rato. El mensaje incluye cuánto le queda a la cookie vigente (o si ya no queda ninguna) para que el usuario sepa la urgencia. Probado en vivo end-to-end: el mensaje de prueba llegó a Telegram.
- **Importante — el disparo de la alerta NO vigila que el API esté respondiendo.** Solo se dispara si `cf_refresher.py` efectivamente corre y falla en resolver el challenge. Si nada dispara el script (ver más abajo, timer pendiente), no hay alerta aunque el API ya esté devolviendo 403 hace rato — la cookie simplemente vence en silencio.

### `mi_api_mcp.py` (nuevo) — MCP server para MI-API
Mismo patrón que los MCP ya existentes en el server (`camaras-ip/mcp_server/camaras_mcp.py`, `hermes_animecast_scraper/jkanime_relator_mcp.py`, `wordpress_blog_animecast/animecast_blog_mcp.py`): un solo archivo, `mcp.server.fastmcp.FastMCP`, stdio, `@mcp.tool()`. **Nota de compatibilidad:** el paquete `mcp` v2.x renombró `FastMCP` a `MCPServer` y rompe este patrón — hubo que fijar `mcp[cli]==1.28.1` en `requirements.txt` (misma versión que usan los otros proyectos) para que `from mcp.server.fastmcp import FastMCP` siga funcionando.

Tools expuestas:
- `estado_cf_clearance()` — chequea TTL restante y hace cuánto se actualizó la cookie en Redis.
- `refrescar_cf_clearance()` — dispara `cf_refresher.py` en background (`subprocess.Popen(["xvfb-run", "-a", VENV_PYTHON, CF_REFRESHER], ...)`), mismo patrón que `verificar_episodios_nuevos_jkanime()` en jkanime_relator_mcp.py.

**Registro:** agregada la entrada `mi_api` en `~/.hermes/config.yaml` bajo `mcp_servers:` (mismo formato que las demás — `command`/`args`/`timeout`), apuntando al Python del propio venv de MI-API. Se corrió `systemctl --user restart hermes-gateway` para activarlo (autorizado por el usuario). Verificado con `hermes mcp test mi_api`: conecta y descubre las 2 tools.

### Pendiente — timer systemd para correr `cf_refresher.py` solo
Se crearon (en la raíz del repo, todavía no instalados en `/etc/systemd/system/`):
- `mi-api-cf-refresh.service` — `Type=oneshot`, corre `xvfb-run -a <venv>/bin/python cf_refresher.py`.
- `mi-api-cf-refresh.timer` — cada 15 min (`OnUnitActiveSec=15min`), `OnBootSec=2min`, `Persistent=true`.

Falta que el usuario copie ambos a `/etc/systemd/system/`, `daemon-reload`, y `enable --now mi-api-cf-refresh.timer` (requiere sudo, no lo pude correr yo). **Hasta que ese timer no esté activo, nada refresca la cookie solo** — el pipe se va a volver a caer en 403 cuando la cookie actual venza, sin ninguna alerta (la alerta vive adentro de `cf_refresher.py`, que no corre si nada lo dispara).

---

### Incidente (misma sesión, después de lo de arriba): el API se cayó de nuevo mientras se armaba el timer

**Contexto:** justo cuando se estaba por instalar el timer, el usuario preguntó "¿cómo opera exactamente el refresh, incondicional o solo si está por vencer?" — se decidió que solo refresque si el TTL de Redis está por debajo de `MIN_TTL_BEFORE_REFRESH_SECONDS` (10 min), para no lanzar Chromium 96 veces/día. Mientras se implementaba eso, el usuario reportó que el API había dejado de responder de nuevo.

**Primer error propio en el diagnóstico:** se probó `/episodes/21` y devolvió 200, dando falsa confianza de que todo estaba bien. **Era un hit de la caché propia de `/episodes/{id}` (`CACHE_EPISODES_HOURS`, 1h TTL)** — esa ID se había pedido tantas veces durante las pruebas de esta sesión que quedó cacheada desde antes de que el pipe empezara a fallar de nuevo. El usuario lo notó ("me imagino que estás leyendo caché maldita sea") antes que yo. Probar con IDs nunca antes pedidas (`66`, `178789`, `196187`, etc.) confirmó que el pipe fallaba con 403 para *todo* lo que no estuviera cacheado — la lección: para validar que el pipe realmente responde, siempre probar con un ID/endpoint que no tenga caché de por medio (`/watch/...` no tiene caché, es la prueba más confiable).

**Causa raíz real (encontrada en 3 pasos, cada uno descartando una hipótesis):**

1. *Hipótesis descartada — cookie vencida:* se forzó un refresh nuevo de `cf_refresher.py` (borrando la key de Redis) y el pipe **seguía fallando incluso con una cookie recién emitida**, in cluso replicada con `curl` plano (sin ningún cliente Python de por medio). Esto descartó "la cookie expiró" como explicación.
2. *Hipótesis descartada — el `cf_clearance` no cubre el endpoint del pipe:* la teoría era que `cf_refresher.py` solo resolvía el challenge contra la home (`/`) y que el pipe (`/api/secure/pipe`) tendría una regla de Cloudflare distinta. Se probó pidiendo una cookie **manual** nueva al usuario (generada en su propio dispositivo, navegando en real a miruro.to) y el mismo patrón se repitió: por `curl` plano, con la query de `episodes` codificada a mano, la request funcionaba perfecto (200). Por `api.py` (vía `curl_cffi`), la misma cookie fallaba. Esto acotó el problema a algo específico de cómo Python arma la request, no a la cookie ni al endpoint.
3. **Causa real:** se replicó la request exacta con `httpx` (sin `curl_cffi` de por medio) y **también falló** — pero con `httpx.AsyncClient(http2=True)` explícito, funcionó (200, `http_version: HTTP/2`). **El pipe de Miruro exige HTTP/2** (como negocia cualquier navegador real y también el `curl` del sistema por default) — cualquier request sobre HTTP/1.1 (el default de `httpx`, y lo que hacía `curl_cffi` con `impersonate=chrome110`) se rechaza con 403 pese a que la cookie y los headers sean idénticos byte a byte. No era un tema de cookie, ni de TLS fingerprint, ni de headers — era el protocolo HTTP en sí.

**Qué se implementó (fix definitivo):**
- `_pipe_get()` en `api.py`: cuando hay un blob de `cf_clearance` en Redis, la request se hace con `httpx.AsyncClient(timeout=20, http2=True)` en vez de con la sesión `curl_cffi` (`pipe_session`). Requiere el paquete `h2` (`pip install "httpx[http2]"`) — agregado a `requirements.txt` como `httpx[http2]`.
- El camino viejo (`curl_cffi`/`pipe_session`) queda como fallback solo para cuando NO hay ninguna cookie en Redis (deja de usarse en la práctica, pero no se borró — no hace daño mantenerlo).
- Verificado en vivo con 3 IDs nunca antes pedidas + un `/watch/...` sin caché: los 4 devolvieron 200 recién restablecido el servicio.

**Además, a pedido del usuario durante el incidente, se endureció todo el sistema de detección/alerta:**

1. **Detección reactiva (no solo el timer):** nueva función `_trigger_reactive_cf_refresh()` en `api.py`. Cuando `_pipe_get()` recibe un 403 por el camino de `cf_clearance`, dispara en el momento (no espera al timer) `cf_refresher.py --force` en background vía `subprocess.Popen(["xvfb-run", ...])`. Un lock en Redis (`miruro_api:cf_refresher:reactive_trigger_lock`, TTL 60s) evita que varias requests fallando al mismo tiempo lancen varios navegadores — y como el lock vive en el Redis **compartido por los 5 nodos**, también deduplica across todo el fleet, no solo dentro de un proceso. Se agregó el flag `--force` a `cf_refresher.py` (bypassea el chequeo de TTL que decide si hace falta refrescar).
2. **Sin debounce en la alerta de fallo — a pedido explícito del usuario** ("quiero spam al maldito telegram... hasta que resuelva, es un sistema crítico"): se sacó el debounce de 1h que tenía `cf_refresher.py` (`_alert_once`/`REDIS_KEY_LAST_ALERT` — eliminados del código, ya no existen). Ahora manda un mensaje cada vez que falla un intento forzado — en la práctica cada ~60s mientras la caída persista (limitado solo por el lock del punto 1, no por ningún cooldown de la alerta en sí).
3. **Notificación desde los 5 nodos, no solo desde casa:** el usuario reveló que este servicio corre en 5 nodos detrás de un balanceador (este equipo físico + 4 nodos cloud), y **solo este equipo tiene Hermes instalado** (el framework de agente que manda a Telegram). Se agregó:
   - `POST /internal/notify` en `api.py` — recibe `{"message": "..."}`, protegido por el mismo `x-api-key` de siempre (no está en la lista de bypass de `secure_api`), y llama a `_notify_telegram()` internamente.
   - `_notify_telegram()` (api.py) y `notify_telegram()` (cf_refresher.py) ahora chequean primero si existe el binario local de Hermes (`/home/carlos-esteven/.hermes/hermes-agent/venv/bin/hermes`); si no existe (los 4 nodos cloud), en vez de fallar en silencio, hacen un POST a `f"{NOTIFY_RELAY_URL}/internal/notify"` (nueva env var) autenticado con el mismo `API_KEY` de siempre.
   - Confirmado con el usuario: los 5 nodos comparten Redis y el mismo `API_KEY`. Este equipo es alcanzable desde los nodos cloud vía **ZeroTier** en `10.147.19.131:8848` (confirmado con `ip addr` — interfaces `ztr2qybuq2`/`ztrfyjgylc`). Falta que el usuario agregue `NOTIFY_RELAY_URL=http://10.147.19.131:8848` al `.env` de los 4 nodos cloud y los reinicie — sin eso, esos 4 nodos no pueden avisar por Telegram si son ellos los que detectan la falla.

**Estado al cierre de esta sesión:**
- API funcionando, verificado con IDs frescos sin caché.
- Fix de HTTP/2 aplicado y probado.
- Detección reactiva + alerta sin debounce aplicadas y probadas (`/internal/notify` responde 200 con key válida, 403 sin key).
- **Pendiente (requiere acción del usuario, no lo pude hacer yo por falta de sudo/acceso a los otros nodos):**
  1. Instalar el timer systemd (`mi-api-cf-refresh.service`/`.timer`, en la raíz del repo) — ver sección anterior.
  2. Agregar `NOTIFY_RELAY_URL=http://10.147.19.131:8848` al `.env` de los 4 nodos cloud y reiniciarlos.
  3. Confirmar que `patchright`/Xvfb estén instalados en los nodos cloud si se espera que ellos también puedan resolver el challenge (si no, dependen de que el lock reactivo lo gane un nodo que sí pueda — típicamente este equipo de casa).

### Ajustes finales de la misma sesión, después de revisar el diseño con el usuario

- **Se descartó el timer por completo** (nunca llegó a instalarse — quedó confirmado con `systemctl list-timers` que no existía). El usuario notó que con TTL=25min, chequeo cada 15min y umbral de 10min, la resta da exactamente 10 (`25-15`), que no es mayor a 10 — en la práctica el timer hubiera terminado refrescando casi en cada tick de todos modos, un patrón fijo cada ~15 min que hace más fácil que Cloudflare lo detecte como bot. Se borraron `mi-api-cf-refresh.service` y `mi-api-cf-refresh.timer` del repo. **El sistema queda 100% reactivo: cero navegadores lanzados si nadie usa el API o si la cookie sigue sirviendo.**
- **Validación (canary) antes de disparar el refresh forzado:** se agregó `_cf_clearance_actually_broken()` en `api.py`. Antes de esto, CUALQUIER 403 disparaba el refresh — pero se confirmó en vivo que los IDs `154587` y `269` daban 403 con una cookie perfectamente sana, simplemente porque esos animes no existen en el catálogo de Miruro (nada que ver con Cloudflare). Ahora, antes de lanzar el navegador, se reintenta con una query conocida y estable (`episodes`, anilistId 21 — One Piece, confirmado que sigue andando en todo lo demás de hoy); si ESA también falla, recién ahí se considera que la cookie está realmente muerta y se dispara el resto del flujo (lock + alerta + `cf_refresher.py --force`). Si la query canary responde bien, no se hace nada — el 403 original no era un problema de cookie.
- **`NODE_ID`** (nueva env var, `api.py` y `cf_refresher.py`): permite ponerle un nombre legible a cada nodo (ej. `NODE_ID=cloud-1`), que se antepone a los mensajes de Telegram como `[nodo: ...]`. Si no está seteada, cae a `socket.gethostname()`. En este equipo se seteó `NODE_ID=server_test_casa`. Verificado en vivo: se carga bien desde `.env` y el proceso reiniciado ya lo tiene.

### Segundo incidente el mismo día: Cloudflare escaló más, y un hueco en el canary

Después de probar el nodo cloud (10.147.19.193/10.147.20.193, mismo equipo por dos redes ZeroTier — confirmado con `/health` respondiendo igual en ambas IPs), se detectó que **`/episodes` funcionaba pero `/watch` (cualquier anime/provider) daba 403 de forma sistémica** — no era un slug puntual. El canary agregado antes solo probaba la ruta `episodes`, así que consideraba "la cookie está bien" aunque `sources` (la ruta que usa `/watch`) estuviera rota, y no disparaba nada.

**Fix:** `_cf_clearance_actually_broken()` ahora prueba **dos** canarios — `episodes` (anilistId 21) y, si ese pasa, `sources` (episodio 1 de One Piece por `ally`, obtenido dinámicamente del resultado del canario de `episodes` para no depender de un ID crudo hardcodeado que se pudiera desactualizar). Hace las llamadas crudas por `httpx` directo (no vía `_pipe_get`/`_fetch_raw_episodes`) para evitar recursión — esas funciones podrían volver a llamar a `_trigger_reactive_cf_refresh()` en un 403.

Después de esto, Cloudflare escaló todavía más mientras el usuario estaba afuera (~30 min): `cf_refresher.py --force` llegó a fallar directamente ("cf_clearance never showed up after 45s — Cloudflare may have escalated to a harder challenge (interactive Turnstile)"), y en un punto ni `episodes` respondía ya, con cookies recién renovadas por la automatización que igual no servían para nada. Se resolvió con otra cookie manual del usuario (tercera vez en el día) — aplicada y confirmada funcionando en ~1 min desde que la mandó.

### Medición real de tiempo de recuperación (a pedido del usuario — "eso de 1 minuto me lo paso por el culo, quiero saber cuánto tarda de verdad")

Se agregó instrumentación real, no estimaciones:
- `api.py` → `_trigger_reactive_cf_refresh()`: al confirmar (vía los dos canarios) que la cookie está rota de verdad, guarda `time.time()` en Redis (`miruro_api:cf_refresher:break_detected_at`, `SET NX EX 3600` — solo la primera vez de una caída continua, no en cada reintento) y manda la alerta de "rechazó la cookie" con hora legible (`_now_str()`, `HH:MM:SS`).
- `cf_refresher.py` → al lograr un refresh exitoso, lee esa misma key; si existe, calcula `elapsed = time.time() - break_detected_at`, lo imprime a consola (`RECOVERY TIME: X.Xs (medido, no estimado)`), manda un Telegram `✅ ... recuperado. Tiempo real roto→arreglado: Xs` y borra la key. Si no existía (fue un refresh manual/proactivo sin caída detectada), no manda ningún mensaje de "recuperado" — no se reporta una recuperación que no pasó.
- Pendiente de ver en la próxima caída real si el tiempo medido coincide con lo esperado (~15-30s cuando Cloudflare está en su nivel normal de challenge).

### Fallback Mac + escalamiento (a pedido del usuario) y dos bugs reales encontrados probándolo

Se armó el segundo nivel de respaldo con el Mac del usuario, descrito y diseñado en la conversación (ver historial). Todo versionado en el repo — nada suelto fuera de git, a pedido explícito del usuario.

**`api.py` — aviso al Mac + escalamiento:**
- `_trigger_reactive_cf_refresh()` ahora, además de disparar `cf_refresher.py --force`, también hace `SET miruro_api:need_mac_refresh EX 600` (bandera durable) y `PUBLISH miruro_api:mac_refresh_channel` (reacción instantánea si el Mac está escuchando). Ambos se disparan EN PARALELO con el intento de Linux, no después de que falle — no hay razón para esperar.
- `_escalate_if_still_broken()` (`asyncio.create_task`, programada en el momento de la rotura): espera `MAC_ESCALATION_TIMEOUT_SECONDS` (120s) y chequea si `break_detected_at` sigue existiendo. Si sigue ahí — ni Linux ni el Mac lo arreglaron — manda alerta 🔴 de escalamiento. Deliberadamente chequea el resultado real (¿la cookie sigue rota?) en vez de si el Mac "confirmó recibido" — un ack no prueba que Chrome resolvió el challenge.
- Probado en vivo: se confirmó que el mensaje de pub/sub efectivamente llega (suscriptor de prueba lo recibió), que `break_detected_at`/`need_mac_refresh` se setean bien en una rotura real (403 con cookie corrupta a propósito), y que el timing de la ventana de 120s es correcto (se verificó que a los ~97s todavía no había disparado, dentro de la ventana esperada).

**Bug #1 encontrado probando: `cf_refresher.py` reportaba "recuperado" con una cookie que no servía.** Después de una rotura simulada, `break_detected_at` se limpió (o sea, cf_refresher.py declaró éxito) pero la siguiente request a `/watch` siguió dando 403 — y poco después `/episodes` también volvió a fallar con la cookie "recién refrescada". La causa: `cf_refresher.py` solo verificaba que **consiguiera alguna** cookie de la home (`_solve_challenge_and_capture` exitoso), sin comprobar que esa cookie realmente sirviera contra el pipe real — el mismo tipo de brecha que ya se había cerrado del lado de `api.py` (el canary de dos rutas), pero nunca se replicó en `cf_refresher.py`.

**Fix:** nueva función `_cookie_actually_works(cookie_str, headers)` en `cf_refresher.py`, mismo patrón de dos canarios que `api.py` (`episodes` anilistId 21, después `sources` con el episodio real de `ally/sub` que devuelve ese canario, decodificando primero el id base64 de Miruro con la misma lógica que `_translate_id` en `api.py`). Se llama después de `_solve_challenge_and_capture()` y antes de aceptar el resultado — si falla, se trata exactamente igual que si el challenge nunca se hubiera resuelto (alerta de fallo, no se toca Redis, `sys.exit(1)`). Verificado en vivo: con el sistema todavía roto, `cf_refresher.py --force` ahora falla honestamente ("Cloudflare may have escalated...") en vez de reportar un falso éxito.

**Bug #2 (no del código, un malentendido de timing en las pruebas):** después de aplicar una cookie manual nueva, las primeras pruebas seguían dando 403 durante unos segundos aunque la cookie ya funcionaba (confirmado pegándole directo al pipe con la misma cookie: 200). Causa: `CF_CLEARANCE_LOCAL_CACHE_SECONDS` (cache en memoria del proceso, para no pegarle a Redis en cada request) estaba en 15s — las pruebas se hicieron demasiado rápido después del push, contra la copia vieja en memoria. **Se bajó a 3s** — dado que es un servicio crítico, el costo extra de un roundtrip a Redis casi en cada request es aceptable a cambio de no tener esta ambigüedad de nuevo.

**`mac_agent/`** (nuevo directorio, todo commiteado — nada vive fuera del repo):
- `refresher.py`: escucha `miruro_api:mac_refresh_channel` (pub/sub, reacción instantánea) y además sondea `miruro_api:need_mac_refresh` cada `MAC_AGENT_POLL_INTERVAL_SECONDS` (default 30 min, red de seguridad si el Mac estaba dormido/sin red cuando se publicó). Al disparar, usa **Playwright normal (no patchright)** manejando el **Chrome real instalado** (`channel="chrome"`, no el Chromium embebido) con un perfil dedicado y descartable (`mac_agent/.chrome-profile/`, gitignored) para no interferir con el uso normal del navegador. Escribe `{cookie, headers, updated_at, source: "mac_agent"}` en el mismo Redis compartido, y si había una rotura pendiente (`break_detected_at`), reporta el tiempo real de recuperación por Telegram igual que hace `cf_refresher.py` — vía el mismo mecanismo de relay (`NOTIFY_RELAY_URL` → `/internal/notify`), porque el Mac tampoco tiene Hermes instalado.
- `requirements.txt`, `.env.example` (plantilla, el `.env` real queda gitignored igual que en el resto del proyecto), y `com.mi-api.mac-refresher.plist` (unidad `launchd`, con rutas placeholder que hay que reemplazar por la ruta real del clone en el Mac).
- Documentado el setup completo paso a paso en `CLAUDE.md`, sección "`mac_agent/` — second-tier cf_clearance fallback, runs on a Mac".
- **Pendiente:** instalar esto de verdad en el Mac del usuario y probarlo en vivo (lanzar el navegador, conseguir cookie real, confirmar que se publica en Redis y que la alerta de recuperación llega). No se ha corrido nada de esto todavía fuera de la revisión de código y el syntax-check — este servidor es Linux, no puede ejecutar la parte de Chrome real de `mac_agent`.

**Actualización — se probó en vivo y funcionó, con un hallazgo nuevo (caché de Cloudflare) encontrado en el proceso.**

El usuario corrió `mac_agent/refresher.py` en su Mac real (venv, `.env` propio, escuchando el canal pub/sub). Desde el servidor se simuló una rotura (`SET break_detected_at` + `SET need_mac_refresh` + `PUBLISH`) y **el Mac reaccionó solo**: abrió Chrome, resolvió el challenge, y escribió la cookie en Redis (`fuente: mac_agent` confirmado). `break_detected_at`/`need_mac_refresh` se limpiaron solos. Primer end-to-end exitoso del flujo completo servidor↔Mac.

**Pero:** la cookie que consiguió el Mac solo servía para `episodes`, no para `sources`/`watch` — el mismo bug que ya se había corregido en `cf_refresher.py` (Bug #1 arriba) pero nunca se portó a `mac_agent/refresher.py`, que seguía aceptando cualquier cookie sin verificarla contra el pipe real. **Pendiente real:** portar `_cookie_actually_works()` de `cf_refresher.py` a `mac_agent/refresher.py` (no se hizo todavía en esta sesión — se priorizó restablecer el servicio primero con una cookie manual).

**Simplificación de `.env` a pedido del usuario ("no quiero doble .env"):** `mac_agent/refresher.py` cargaba su propio `mac_agent/.env` — se cambió para que lea el `.env` de la raíz del repo (`Path(__file__).resolve().parent.parent / ".env"`), el mismo que ya usa `api.py`. Se plegaron `NODE_ID`, `NOTIFY_RELAY_URL` y `MAC_AGENT_POLL_INTERVAL_SECONDS` al `.env_example` de la raíz y se borró `mac_agent/.env_example` (quedaba redundante). Un solo `.env` por máquina, no uno por componente.

### `windows_agent/` (nuevo) — probar si un equipo Windows (cloud/físico) sirve como fuente alternativa de cf_clearance

A pedido del usuario, tras el problema del Mac (cookie parcial): un script de diagnóstico standalone, **sin integrar al flujo reactivo todavía** — no escribe a Redis, no notifica a nadie. `windows_agent/test_cookie.py`: resuelve el challenge con el Chrome real instalado (mismo enfoque que `mac_agent`, sin Xvfb) y prueba la cookie contra los endpoints reales de producción (`/recent-episodes` → path `schedule`, `/watch` → paths `episodes` luego `sources`), no contra un canario inventado. Imprime PASS/FAIL.

### Hallazgo importante: Cloudflare cachea las respuestas del pipe — los canarios podían dar falso positivo

Mientras se armaba `windows_agent`, el usuario preguntó específicamente "¿tiene control de que no dé falsos positivos por caché?" — buena pregunta, la respuesta era que no, y se confirmó en vivo que era un problema real:

- `path: "schedule"` (recent-episodes): `cf-cache-status: HIT`, `age: 75255` (**20+ horas** de antigüedad).
- `path: "episodes"`, anilistId 21 (el canario usado en `api.py` y `cf_refresher.py` TODO el día): también `cf-cache-status: HIT`, `age: 11068` (**3+ horas**).

Un `HIT` de caché de Cloudflare **nunca llega al backend de Miruro** — significa que los dos canarios que se habían estado usando en `_cf_clearance_actually_broken()` (api.py) y `_cookie_actually_works()` (cf_refresher.py) durante buena parte del día podían estar devolviendo "la cookie sirve" sin haber validado nada de verdad contra el origen. Esto probablemente explica parte de la confusión de hoy (episodes "pasando" con cookies que después resultaban no servir para nada).

**Fix (aplicado en los 3 lugares — `api.py`, `cf_refresher.py`, `windows_agent/test_cookie.py`):** se agregó un campo random (`_cache_bust()`, 8 letras al azar) a la `query` de cada canario/prueba. Confirmado en vivo: agregar ese campo (que Miruro ignora, sigue respondiendo 200 con datos válidos) cambia la cache key en Cloudflare y fuerza `cf-cache-status: MISS` — o sea, sí llega al backend real cada vez. **Importante — alcance acotado:** esto SOLO afecta las queries internas de verificación/canario. El tráfico real de usuarios (`/episodes`, `/recent-episodes`, `/watch` servidos por la API) sigue cacheando normal tanto en Cloudflare como en la caché propia de Redis (`CACHE_EPISODES_HOURS`, etc.) — no se desactivó caching de producción en ningún lado, el usuario lo preguntó explícitamente y se confirmó que no.

**Pendiente:** no se ha corrido `windows_agent/test_cookie.py` todavía en una máquina Windows real (este servidor es Linux). El usuario lo va a probar en un escritorio cloud Windows.

### Se portó la verificación (`_cookie_actually_works`) a `mac_agent/refresher.py`

Quedaba pendiente desde la prueba en vivo del Mac (arriba): el script aceptaba cualquier cookie que consiguiera sin comprobar que sirviera contra `episodes` + `sources`. Se agregó la misma función `_cookie_actually_works()` (con el cache-bust ya incluido desde el arranque, a diferencia de `cf_refresher.py`/`api.py` que lo tuvieron que agregar después) — se llama justo después de `_solve_challenge_and_capture()` y antes de escribir nada en Redis. Si falla, se trata igual que si el challenge nunca se hubiera resuelto: alerta por Telegram (vía el relay, `NOTIFY_RELAY_URL`), no se toca `miruro_api:cf_clearance` ni se limpia `break_detected_at`. No se pudo probar en vivo en esta sesión (requiere el Mac real corriendo); syntax-check OK.
