import asyncio, base64, json, gzip, httpx, os, socket, subprocess, time
from pathlib import Path
from curl_cffi.requests import AsyncSession as CurlSession
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Miruro API", version="2.0")

# --- Security Configuration ---
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "").split(",")

# --- API Key Header Name ---
API_KEY_NAME = "x-api-key"

# --- API Key Configuration ---
VALID_API_KEY = os.getenv("API_KEY")

# --- Debug Configuration ---
API_DEBUG = os.getenv("API_DEBUG", "False").lower() == "true"

# --- Redis Cache Configuration ---
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = os.getenv("REDIS_PORT")
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None
REDIS_ENABLED = bool(REDIS_HOST and REDIS_PORT)

# --- Cache TTLs (in seconds) ---
CACHE_RECENT_EPISODES_HOURS = int(os.getenv("CACHE_RECENT_EPISODES_HOURS", "2"))
CACHE_RECENT_EPISODES_TTL = CACHE_RECENT_EPISODES_HOURS * 3600  # seconds
CACHE_EPISODES_HOURS = int(os.getenv("CACHE_EPISODES_HOURS", "1"))
CACHE_EPISODES_TTL = CACHE_EPISODES_HOURS * 3600  # seconds

# --- Redis Cache Keys ---
REDIS_KEY_RECENT_EPISODES = "miruro_api:cache:recent_episodes"
REDIS_KEY_EPISODES_PREFIX = "miruro_api:cache:episodes"

# Isolates fallback groups end to end — the cf_clearance cookie itself, not just who gets
# notified of a break. Two groups sharing one cookie meant either could "rescue" the other, but
# it also meant a node's own local cache staleness (CF_CLEARANCE_LOCAL_CACHE_SECONDS) could make
# it look like group B's fix was "caused by" group A firing seconds apart — actually just two
# independent nodes racing against the same shared cookie. Full isolation: each group solves,
# holds, and lives or dies by its own cookie. Set the SAME FALLBACK_TOPIC here and on every
# --listen node meant to answer for this production node; a different topic never shares a
# cookie, notification channel, or anything else with this one. Defaults to "default" (one
# shared pool) if unset.
FALLBACK_TOPIC = os.getenv("FALLBACK_TOPIC", "default")
REDIS_KEY_CF_CLEARANCE = f"miruro_api:cf_clearance:{FALLBACK_TOPIC}"

# How long to trust an in-memory copy of the cf_clearance blob before re-checking Redis. Kept
# short on purpose — a stale in-memory copy right after a manual push looked exactly like a
# still-broken service during testing (confirmed live: same cookie, 403 through the cached
# in-memory copy, 200 straight from Redis/direct httpx). This is a critical service; a Redis
# round-trip on every pipe request is a cost worth paying to not have that ambiguity.
CF_CLEARANCE_LOCAL_CACHE_SECONDS = 3

# --- Blocked Episode Source Prefixes ---
# Comma-separated list of episode ID prefixes (the part before ':', e.g. "animepahe") to hide
# from /episodes responses. Use this to hide a source that's temporarily broken/hanging upstream
# without touching code — just edit the env var and restart.
BLOCKED_EPISODE_PREFIXES = {
    p.strip() for p in os.getenv("BLOCKED_EPISODE_PREFIXES", "").split(",") if p.strip()
}

# --- Miruro Pipe Configuration ---
MIRURO_BASE_URL = os.getenv("MIRURO_BASE_URL").rstrip("/")

# --- Pipe Request Configuration ---
PIPE_USER_AGENT = os.getenv("PIPE_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
PIPE_EXTRA_HEADERS = json.loads(os.getenv("PIPE_EXTRA_HEADERS", "{}"))
PIPE_IMPERSONATE = os.getenv("PIPE_IMPERSONATE")

# --- Upstream URLs & Headers ---
HEADERS = {
    "User-Agent": PIPE_USER_AGENT,
    "Referer": f"{MIRURO_BASE_URL}/",
    **PIPE_EXTRA_HEADERS,
}

# --- AniList GraphQL Endpoint ---
ANILIST_URL = "https://graphql.anilist.co"

# --- Miruro Pipe Endpoint ---
MIRURO_PIPE_URL = f"{MIRURO_BASE_URL}/api/secure/pipe"

redis_client = aioredis.Redis(
    host=REDIS_HOST,
    port=int(REDIS_PORT),
    password=REDIS_PASSWORD,
    decode_responses=True,
    socket_connect_timeout=3,
    socket_timeout=3,
) if REDIS_ENABLED else None

pipe_session = CurlSession(impersonate=PIPE_IMPERSONATE)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def secure_api(request: Request, call_next):
    # Allow home page (docs) without restrictions
    if request.url.path in ["/", "/docs", "/redoc", "/openapi.json", "/health"]:
        return await call_next(request)

    # 1. Check API Key
    api_key = request.headers.get(API_KEY_NAME)
    if VALID_API_KEY and api_key == VALID_API_KEY:
        return await call_next(request)

    # 2. Check Origin or Referer
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")

    is_allowed = False
    for allowed in ALLOWED_ORIGINS:
        if (origin and origin.startswith(allowed)) or (referer and referer.startswith(allowed)):
            is_allowed = True
            break
            
    if not is_allowed:
        return JSONResponse(
            status_code=403,
            content={"detail": "Access forbidden: Invalid Origin, Referer, or API Key."}
        )

    return await call_next(request)

def _proxy_img(url: str) -> str:
    # Proxy removed — return original image URL
    return url


def _proxy_deep_images(obj):
    # Proxy removed — return data unchanged
    return obj

def _filter_blocked_prefixes(data: dict) -> dict:
    """Remove episodes whose ID prefix (the part before ':') is in BLOCKED_EPISODE_PREFIXES.
    Configured via the BLOCKED_EPISODE_PREFIXES env var — no code changes needed to add/remove
    a blocked source."""
    if not BLOCKED_EPISODE_PREFIXES:
        return data
    for provider_data in data.get("providers", {}).values():
        if not isinstance(provider_data, dict):
            continue
        episodes = provider_data.get("episodes", {})
        if not isinstance(episodes, dict):
            continue
        for category, ep_list in episodes.items():
            if not isinstance(ep_list, list):
                continue
            episodes[category] = [
                ep for ep in ep_list
                if not (isinstance(ep, dict) and isinstance(ep.get("id"), str)
                        and ep["id"].split(":")[0] in BLOCKED_EPISODE_PREFIXES)
            ]
    return data


def _inject_source_slugs(data: dict, anilist_id: int):
    """Transform episode IDs into simplified path-based slugs: watch/PROV/ALID/CAT/PREFIX-NUMBER"""
    providers = data.get("providers", {})
    for provider_name, provider_data in providers.items():
        if not isinstance(provider_data, dict):
            continue
        episodes = provider_data.get("episodes", {})
        if not isinstance(episodes, dict):
            # Some providers return a flat list — wrap it
            if isinstance(episodes, list):
                provider_data["episodes"] = {"sub": episodes}
                episodes = provider_data["episodes"]
            else:
                continue
        for category, ep_list in episodes.items():
            if not isinstance(ep_list, list):
                continue
            for ep in ep_list:
                if not isinstance(ep, dict):
                    continue
                if "id" in ep and "number" in ep:
                    orig_id = ep["id"]
                    prefix = orig_id.split(":")[0] if ":" in orig_id else orig_id
                    ep["id"] = f"watch/{provider_name}/{anilist_id}/{category}/{prefix}-{ep['number']}"
    return data

async def _cache_get(key: str):
    """Fetch a cached JSON value from Redis. Returns None if Redis isn't configured, unreachable, or the key is missing."""
    if not REDIS_ENABLED:
        return None
    try:
        cached = await redis_client.get(key)
        return json.loads(cached) if cached else None
    except Exception:
        return None


async def _cache_set(key: str, value, ttl: int):
    """Store a JSON value in Redis. No-op if Redis isn't configured or unreachable."""
    if not REDIS_ENABLED:
        return
    try:
        await redis_client.setex(key, ttl, json.dumps(value))
    except Exception:
        pass


_cf_clearance_local_cache = {"data": None, "fetched_at": 0.0}

async def _get_pipe_headers() -> dict:
    """Build headers for a pipe request. When cf_refresher.py has published a solved
    `cf_clearance` cookie to Redis, use ITS header set verbatim (plus the cookie) — those are
    the exact browser headers Cloudflare saw when the challenge was solved, so they must not be
    mixed key-for-key with the static HEADERS (e.g. "User-Agent" vs "user-agent" would otherwise
    both get sent as distinct dict keys, which no real browser does and re-triggers the
    challenge). Falls back to the static HEADERS when Redis has nothing (or is unreachable)."""
    now = time.time()
    cached = _cf_clearance_local_cache
    if now - cached["fetched_at"] > CF_CLEARANCE_LOCAL_CACHE_SECONDS:
        blob = await _cache_get(REDIS_KEY_CF_CLEARANCE)
        cached["data"] = blob
        cached["fetched_at"] = now

    blob = cached["data"]
    if not blob:
        return HEADERS

    merged = dict(blob.get("headers", {}))
    merged["cookie"] = blob["cookie"]
    merged.setdefault("referer", HEADERS.get("Referer"))
    return merged

# --- Reactive cf_clearance recovery ---
# A 403 through the cf_clearance/httpx path means the cached cookie just failed a REAL request
# — stronger and faster evidence than "Redis TTL says it's still young" (that already burned us
# once: a cookie can die well before its TTL). On that signal we kick a forced re-solve in the
# background immediately, instead of waiting for the next timer tick (up to 15 min away).
BASE_DIR = Path(__file__).resolve().parent
CF_REFRESHER_TRIGGER_LOCK_KEY = "miruro_api:cf_refresher:reactive_trigger_lock"
CF_REFRESHER_TRIGGER_LOCK_TTL = 60  # de-dupes concurrent failing requests into one browser run
# Only set on the home node's own .env — no hardcoded path with a real username in the repo.
# Unset (any cloud node, or the home node before Hermes is configured) means os.path.exists("")
# is False, falling through to the NOTIFY_RELAY_URL branch below, same as before.
HERMES_BIN = os.getenv("HERMES_BIN_PATH", "")

# Shared with cf_refresher.py (same literal key) — real, measured break-to-recovery timing
# instead of anyone's guess. See _trigger_reactive_cf_refresh (sets it) and cf_refresher.py's
# main() (reads/clears it and reports the actual elapsed seconds on successful recovery).
REDIS_KEY_BREAK_DETECTED_AT = "miruro_api:cf_refresher:break_detected_at"

# Second-tier fallback: any machine running `cf_refresher.py --listen` (a Mac, an extra Ubuntu
# box, a Windows box — real Chrome, a non-datacenter IP Cloudflare trusts far more than this
# server's, which gets more suspicious the more it auto-solves challenges). REDIS_KEY_NEED_MAC_REFRESH
# is a persistent flag (survives even if a pub/sub message is missed — Redis pub/sub does NOT
# queue messages for offline subscribers, it's fire-and-forget) that every --listen node polls
# every 30-60min as a safety net; REDIS_CHANNEL_MAC_REFRESH is the instant-reaction path when a
# --listen node's listener happens to be connected at that moment.
#
# FALLBACK_TOPIC (defined above, next to REDIS_KEY_CF_CLEARANCE) segments this into isolated
# groups too — only --listen nodes sharing the SAME FALLBACK_TOPIC as THIS node hear its
# triggers, on top of already having their own separate cookie.
REDIS_KEY_NEED_MAC_REFRESH = f"miruro_api:need_mac_refresh:{FALLBACK_TOPIC}"
REDIS_CHANNEL_MAC_REFRESH = f"miruro_api:mac_refresh_channel:{FALLBACK_TOPIC}"
MAC_ESCALATION_TIMEOUT_SECONDS = 120  # grace period before concluding NOBODY fixed it


def _now_str() -> str:
    import datetime
    return datetime.datetime.now().strftime("%H:%M:%S")

# Identifies which of the 5 nodes an alert came from. Set NODE_ID per-node (e.g. "cloud-1") in
# each .env for a human-friendly label; falls back to the OS hostname when unset.
NODE_ID = os.getenv("NODE_ID") or socket.gethostname()

# This app runs on 5 nodes behind a load balancer (this home machine + 4 cloud nodes), but only
# THIS one has Hermes installed (it's the physical machine, Hermes lives on it directly). A
# cloud node has no local Hermes to shell out to, so it relays the message over the shared
# ZeroTier network to THIS node's own /internal/notify instead, which does have Hermes and
# sends it for real. NOTIFY_RELAY_URL should be unset here (home) and set to this machine's
# ZeroTier address (e.g. http://10.x.x.x:8848) in the .env of the 4 cloud nodes.
NOTIFY_RELAY_URL = os.getenv("NOTIFY_RELAY_URL", "").rstrip("/")


def _notify_telegram(message: str) -> None:
    """Best-effort, matches cf_refresher.py's notify_telegram — never let this crash a request."""
    if os.path.exists(HERMES_BIN):
        try:
            subprocess.run(
                [HERMES_BIN, "send", "--to", "telegram", "-q", message],
                timeout=10,
                capture_output=True,
                text=True,
            )
        except Exception:
            pass
        return

    if not NOTIFY_RELAY_URL:
        return  # no local Hermes and no relay configured — nothing else we can do

    try:
        httpx.post(
            f"{NOTIFY_RELAY_URL}/internal/notify",
            json={"message": message},
            headers={"x-api-key": VALID_API_KEY},
            timeout=10,
        )
    except Exception:
        pass


# Known-good, stable canary queries for validating whether cf_clearance itself is actually dead.
# anilistId 21 (One Piece) is about as safe a bet as exists for "this is in Miruro's catalog" —
# confirmed to keep working across everything else that happened today.
#
# IMPORTANT #1: Cloudflare/Miruro evaluate different pipe "path" values independently — confirmed
# live: a cookie can 200 on "episodes" while 403ing on EVERY "sources" query (any anime, any
# provider), and vice versa isn't guaranteed either. One canary against a single path can say
# "cookie's fine" while the actual path a real request needed is silently broken, so this checks
# both of the two paths this app actually uses against the pipe (episodes, sources) — "schedule"
# isn't checked separately, /recent-episodes is low-traffic enough that a live 403 there catching
# it is an acceptable gap for now.
#
# IMPORTANT #2: Miruro's pipe responses are cached at Cloudflare's edge (confirmed live:
# `cf-cache-status: HIT`, `age: 11068` on this exact anilistId=21 episodes query — 3+ HOURS old).
# A cache HIT never reaches Miruro's origin at all, so it says nothing about whether cf_clearance
# actually works — repeatedly "passed" a canary against a cookie that was actually already dead.
# Adding a throwaway random field to the query changes the cache key (confirmed live: same query
# plus one junk field flips `cf-cache-status` from HIT to MISS, still 200) without Miruro's
# backend rejecting the unrecognized field — so every canary call below gets its own cache-busted
# copy of the query, guaranteeing it actually reaches the origin.
_CANARY_SOURCES_ANILIST_ID = 21
_CANARY_SOURCES_PROVIDER = "ally"
_CANARY_SOURCES_CATEGORY = "sub"


def _cache_bust() -> str:
    import random, string
    return "".join(random.choices(string.ascii_lowercase, k=8))


async def _cf_clearance_actually_broken() -> bool:
    """A 403 on one specific request is NOT proof the cookie is dead — confirmed live today:
    anilistId 154587 and 269 both 403 with a perfectly healthy cf_clearance, because Miruro's
    own catalog just doesn't have them (nothing to do with Cloudflare). Re-check with known-
    stable, cache-busted queries before spending a real browser launch on it — only these
    results decide whether the cookie is actually the problem. Does its own raw HTTP calls
    rather than going through _pipe_get/_fetch_raw_episodes — those can themselves call back
    into this function on a 403, and recursing here would spawn refresh attempts, not just check
    for them."""
    headers = await _get_pipe_headers()
    if "cookie" not in headers:
        return True  # nothing cached at all — definitely broken, no canary needed

    async def _raw_pipe_call(payload: dict):
        # A 429 means "rate limited right now" (e.g. several nodes' canary checks landing at
        # once), not "cookie is broken" — confirmed live: a cookie the Mac fallback had just
        # solved fresh got misread as dead this way and triggered a false alarm. Only treat a
        # non-429 failure (403, etc.) as a real signal; a 429 gets a couple of backed-off
        # retries first.
        url = f"{MIRURO_PIPE_URL}?e={_encode_pipe_request(payload)}"
        delay = 3
        async with httpx.AsyncClient(timeout=10, http2=True) as client:
            for attempt in range(3):
                res = await client.get(url, headers=headers)
                if res.status_code != 429:
                    break
                await asyncio.sleep(delay)
                delay *= 2
        if res.status_code != 200:
            return None
        return _decode_pipe_response(res.text.strip())

    episodes_payload = {
        "path": "episodes", "method": "GET",
        "query": {"anilistId": _CANARY_SOURCES_ANILIST_ID, "_cb": _cache_bust()},
        "body": None, "version": "0.1.0",
    }
    try:
        episodes_data = await _raw_pipe_call(episodes_payload)
    except Exception:
        return True  # couldn't even connect — treat as broken, safe default
    if episodes_data is None:
        return True

    try:
        _deep_translate(episodes_data)
        eps = (
            episodes_data.get("providers", {})
            .get(_CANARY_SOURCES_PROVIDER, {})
            .get("episodes", {})
            .get(_CANARY_SOURCES_CATEGORY, [])
        )
        raw_episode_id = eps[0]["id"] if eps else None
        if not raw_episode_id:
            return False  # can't build the sources canary — don't block recovery on it

        sources_payload = {
            "path": "sources",
            "method": "GET",
            "query": {
                "episodeId": base64.urlsafe_b64encode(raw_episode_id.encode()).decode().rstrip("="),
                "provider": _CANARY_SOURCES_PROVIDER,
                "category": _CANARY_SOURCES_CATEGORY,
                "anilistId": _CANARY_SOURCES_ANILIST_ID,
                "_cb": _cache_bust(),
            },
            "body": None,
            "version": "0.1.0",
        }
        sources_data = await _raw_pipe_call(sources_payload)
        return sources_data is None
    except Exception:
        return True


async def _trigger_reactive_cf_refresh() -> None:
    """Fires a forced cf_refresher.py run in the background (deduped via a short Redis lock so
    a burst of concurrently-failing requests doesn't spawn one browser per request) and sends an
    immediate Telegram heads-up. Never raises — this runs from inside an error path."""
    if not await _cf_clearance_actually_broken():
        return  # confirmed via canary: this 403 wasn't about the cookie, nothing to fix

    try:
        got_lock = await redis_client.set(
            CF_REFRESHER_TRIGGER_LOCK_KEY, "1", nx=True, ex=CF_REFRESHER_TRIGGER_LOCK_TTL
        ) if REDIS_ENABLED else True
    except Exception:
        got_lock = True  # Redis hiccup shouldn't block the recovery attempt itself

    if not got_lock:
        return  # another failing request already triggered a refresh moments ago

    # Records the FIRST moment of a continuous outage (NX — later retries within the same
    # outage don't overwrite it) so cf_refresher.py can log/report the real, measured time to
    # recovery instead of anyone eyeballing it. Cleared on successful recovery (see
    # cf_refresher.py); the 1h expiry here is just a safety net against it never getting cleared.
    try:
        await redis_client.set(REDIS_KEY_BREAK_DETECTED_AT, str(time.time()), nx=True, ex=3600)
    except Exception:
        pass

    _notify_telegram(
        f"⚠️ MI-API [nodo: {NODE_ID}]: el pipe de Miruro rechazó la cookie actual "
        f"(403 en vivo, {_now_str()}). Disparando un refresh forzado ahora mismo — si en ~1 min "
        "sigue caído, necesito una cf_clearance nueva desde tu equipo."
    )
    try:
        subprocess.Popen(
            ["xvfb-run", "-a", str(BASE_DIR / "venv" / "bin" / "python"),
             str(BASE_DIR / "cf_refresher.py"), "--force"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass

    # Second-tier fallback, fired alongside the Linux attempt (not after it fails) — no reason
    # to wait and see before asking the Mac too. The flag is the durable ask (mac_agent's 30-60
    # min poll catches it even if the Mac was offline for the instant push below); the publish
    # is just the fast path for whenever the Mac's listener happens to already be connected.
    try:
        await redis_client.set(REDIS_KEY_NEED_MAC_REFRESH, "1", ex=600)
        await redis_client.publish(REDIS_CHANNEL_MAC_REFRESH, "refresh")
    except Exception:
        pass

    # Escalate if NOTHING — not the Linux attempt, not the Mac — actually fixed it in time.
    # Checking the real outcome (is break_detected_at still there) rather than "did the Mac
    # acknowledge" is deliberate: an ack only proves the message arrived, not that the challenge
    # actually got solved (we watched the Linux side fail that exact way earlier today).
    asyncio.create_task(_escalate_if_still_broken())


async def _escalate_if_still_broken() -> None:
    """One-shot, scheduled the moment a break is detected. Never raises."""
    await asyncio.sleep(MAC_ESCALATION_TIMEOUT_SECONDS)
    try:
        still_broken = await redis_client.exists(REDIS_KEY_BREAK_DETECTED_AT)
    except Exception:
        return  # can't tell either way — don't false-alarm on a Redis hiccup
    if still_broken:
        _notify_telegram(
            f"🔴 MI-API [nodo: {NODE_ID}]: siguen sin resolverlo — ni el refresh automático de "
            f"este servidor ni el Mac lo arreglaron en los últimos {MAC_ESCALATION_TIMEOUT_SECONDS}s "
            f"({_now_str()}). Necesito una cf_clearance manual ya."
        )


async def _pipe_get(encoded_req: str) -> dict:
    """GET the pipe and decode the response, replacing the session and retrying once on any
    failure (connection error, non-200 status, or a corrupted/truncated response body)."""
    global pipe_session
    url = f"{MIRURO_PIPE_URL}?e={encoded_req}"
    headers = await _get_pipe_headers()

    # When we have a solved cf_clearance, replay the request with plain httpx over HTTP/2
    # instead of curl_cffi. Confirmed live: the identical cookie/headers succeed via plain
    # system `curl` (which negotiates HTTP/2 by default) and via httpx with http2=True, but
    # fail — same cookie, same headers — over plain HTTP/1.1 (curl_cffi's session, and httpx's
    # own HTTP/1.1 default both 403). Cloudflare's bot-management here is keying off the
    # HTTP/2 vs HTTP/1.1 connection itself (a real browser always negotiates h2 for this kind
    # of XHR), not the header/cookie content — those matched byte-for-byte in every failing
    # attempt too. http2=True requires the `h2` package (see requirements.txt).
    if "cookie" in headers:
        try:
            async with httpx.AsyncClient(timeout=20, http2=True) as client:
                res = await client.get(url, headers=headers)
        except Exception:
            raise HTTPException(status_code=503, detail="Pipe unavailable")
        if res.status_code != 200:
            if res.status_code == 403:
                await _trigger_reactive_cf_refresh()
            status = res.status_code if 100 <= res.status_code <= 599 else 502
            raise HTTPException(status_code=status, detail="Pipe request failed")
        try:
            return _decode_pipe_response(res.text.strip())
        except Exception:
            raise HTTPException(status_code=502, detail="Pipe response corrupted")

    try:
        res = await pipe_session.get(url, headers=headers)
        if res.status_code == 200:
            return _decode_pipe_response(res.text.strip())
    except Exception:
        pass

    stale_session = pipe_session
    pipe_session = CurlSession(impersonate=PIPE_IMPERSONATE)
    try:
        await stale_session.close()
    except Exception:
        pass

    try:
        res = await pipe_session.get(url, headers=headers)
    except Exception:
        raise HTTPException(status_code=503, detail="Pipe unavailable")
    if res.status_code != 200:
        # No cookie was even in Redis to try (that's what put us on this fallback path at
        # all) — a 403 here means the same thing it does on the httpx/cookie path above:
        # nothing usable is cached, kick a forced re-solve instead of staying down silently.
        if res.status_code == 403:
            await _trigger_reactive_cf_refresh()
        status = res.status_code if 100 <= res.status_code <= 599 else 502
        raise HTTPException(status_code=status, detail="Pipe request failed")
    try:
        return _decode_pipe_response(res.text.strip())
    except Exception:
        raise HTTPException(status_code=502, detail="Pipe response corrupted")

async def _fetch_raw_episodes(anilist_id: int) -> dict:
    """Internal helper to fetch raw, decoded episode data from Miruro pipe."""
    payload = {
        "path": "episodes",
        "method": "GET",
        "query": {"anilistId": anilist_id},
        "body": None,
        "version": "0.1.0",
    }
    encoded_req = _encode_pipe_request(payload)
    data = await _pipe_get(encoded_req)
    _deep_translate(data)
    return data

async def _fetch_raw_recents() -> dict:
    """Internal helper to fetch raw, decoded episode data from Miruro pipe."""
    payload = {
        "path": "schedule",
        "method": "GET",
        "query": {"sort":["TIME_DESC"],"newest":True},
        "body": None,
    }
    encoded_req = _encode_pipe_request(payload)
    data = await _pipe_get(encoded_req)
    _deep_translate(data)
    return data

# ─── Shared GraphQL Fragments ────────────────────────────────────────────────

MEDIA_LIST_FIELDS = """
    id
    title { romaji english native }
    coverImage { large extraLarge }
    bannerImage
    format
    season
    seasonYear
    episodes
    duration
    status
    averageScore
    meanScore
    popularity
    favourites
    genres
    source
    countryOfOrigin
    isAdult
    studios(isMain: true) { nodes { name isAnimationStudio } }
    nextAiringEpisode { episode airingAt timeUntilAiring }
    startDate { year month day }
    endDate { year month day }
"""

MEDIA_FULL_FIELDS = """
    id
    idMal
    title { romaji english native }
    description(asHtml: false)
    coverImage { large extraLarge color }
    bannerImage
    format
    season
    seasonYear
    episodes
    duration
    status
    averageScore
    meanScore
    popularity
    favourites
    trending
    genres
    tags { name rank isMediaSpoiler }
    source
    countryOfOrigin
    isAdult
    hashtag
    synonyms
    siteUrl
    trailer { id site thumbnail }
    studios { nodes { id name isAnimationStudio siteUrl } }
    nextAiringEpisode { episode airingAt timeUntilAiring }
    startDate { year month day }
    endDate { year month day }
    characters(sort: [ROLE, RELEVANCE], perPage: 25) {
        edges {
            role
            node { id name { full native } image { large } }
            voiceActors(language: JAPANESE) { id name { full native } image { large } languageV2 }
        }
    }
    staff(sort: RELEVANCE, perPage: 25) {
        edges {
            role
            node { id name { full native } image { large } }
        }
    }
    relations {
        edges {
            relationType(version: 2)
            node {
                id
                title { romaji english native }
                coverImage { large }
                format
                type
                status
                episodes
                meanScore
            }
        }
    }
    recommendations(sort: RATING_DESC, perPage: 10) {
        nodes {
            rating
            mediaRecommendation {
                id
                title { romaji english native }
                coverImage { large }
                format
                episodes
                status
                meanScore
                averageScore
            }
        }
    }
    externalLinks { url site type }
    streamingEpisodes { title thumbnail url site }
    stats {
        scoreDistribution { score amount }
        statusDistribution { status amount }
    }
"""

# ─── Utility Functions ───────────────────────────────────────────────────────

def _translate_id(encoded_id: str) -> str:
    """Decode a base64-encoded episode ID back to plain text."""
    try:
        decoded = base64.urlsafe_b64decode(encoded_id + '=' * (4 - len(encoded_id) % 4)).decode()
        if ':' in decoded:
            return decoded
        return encoded_id
    except Exception:
        return encoded_id


def _deep_translate(obj):
    """Recursively walk a JSON structure and decode any base64 'id' fields."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == 'id' and isinstance(value, str):
                obj[key] = _translate_id(value)
            elif isinstance(value, (dict, list)):
                _deep_translate(value)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _deep_translate(item)


def _decode_pipe_response(encoded_str: str) -> dict:
    """Decode a base64+gzip pipe response into a plain dict."""
    try:
        encoded_str += '=' * (4 - len(encoded_str) % 4)
        compressed = base64.urlsafe_b64decode(encoded_str)
        return json.loads(gzip.decompress(compressed).decode('utf-8'))
    except Exception:
        raise ValueError("Failed to decode pipe response")


def _encode_pipe_request(payload: dict) -> str:
    """Encode a dict into the base64 format expected by the pipe endpoint."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')


async def _anilist_query(query: str, variables: dict = None):
    """Execute an AniList GraphQL query and return the data."""
    body = {"query": query}
    if variables:
        body["variables"] = variables
    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(ANILIST_URL, json=body)
        if res.status_code != 200:
            raise HTTPException(status_code=500, detail="AniList query failed")
        return res.json().get("data", {})


# ─── Homepage ────────────────────────────────────────────────────────────────

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

def _load_template(filename: str) -> str:
    with open(os.path.join(TEMPLATES_DIR, filename), encoding="utf-8") as f:
        return f.read()

@app.get("/", response_class=HTMLResponse)
async def home():
    if API_DEBUG:
        return _load_template("home_debug.html")
    else:
        return _load_template("home_minimal.html")

# ─── Search & Suggestions ───────────────────────────────────────────────────

@app.get("/search")
async def search_anime(
    query: str,
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(20, ge=1, le=50, description="Results per page"),
):
    """Search for anime by name via AniList GraphQL — returns full metadata."""
    gql = f"""
    query ($search: String, $page: Int, $perPage: Int) {{
        Page(page: $page, perPage: $perPage) {{
            pageInfo {{ total currentPage lastPage hasNextPage perPage }}
            media(search: $search, type: ANIME, sort: SEARCH_MATCH) {{
                {MEDIA_LIST_FIELDS}
            }}
        }}
    }}
    """
    data = await _anilist_query(gql, {"search": query, "page": page, "perPage": per_page})
    page_data = data.get("Page", {})
    page_info = page_data.get("pageInfo", {})
    response = {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "results": page_data.get("media", []),
    }
    return _proxy_deep_images(response)


@app.get("/suggestions")
async def search_suggestions(
    query: str = Query(..., min_length=1, description="Search query for autocomplete"),
):
    """Lightweight search for dropdown autocomplete — returns minimal data fast."""
    gql = """
    query ($search: String) {
        Page(page: 1, perPage: 8) {
            media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
                id
                title { romaji english }
                coverImage { large }
                format
                status
                startDate { year }
                episodes
            }
        }
    }
    """
    data = await _anilist_query(gql, {"search": query})
    results = []
    for item in data.get("Page", {}).get("media", []):
        results.append({
            "id": item["id"],
            "title": item["title"].get("english") or item["title"].get("romaji"),
            "title_romaji": item["title"].get("romaji"),
            "poster": item["coverImage"]["large"],
            "format": item.get("format"),
            "status": item.get("status"),
            "year": (item.get("startDate") or {}).get("year"),
            "episodes": item.get("episodes"),
        })
    return _proxy_deep_images({"suggestions": results})


# ─── Advanced Filter ─────────────────────────────────────────────────────────

SORT_MAP = {
    "SCORE_DESC": "SCORE_DESC",
    "POPULARITY_DESC": "POPULARITY_DESC",
    "TRENDING_DESC": "TRENDING_DESC",
    "START_DATE_DESC": "START_DATE_DESC",
    "FAVOURITES_DESC": "FAVOURITES_DESC",
    "UPDATED_AT_DESC": "UPDATED_AT_DESC",
}

@app.get("/filter")
async def filter_anime(
    genre: Optional[str] = Query(None, description="Genre name, e.g. Action, Romance"),
    tag: Optional[str] = Query(None, description="Tag name, e.g. Isekai, Time Skip"),
    year: Optional[int] = Query(None, description="Season year, e.g. 2025"),
    season: Optional[str] = Query(None, description="WINTER, SPRING, SUMMER, or FALL"),
    format: Optional[str] = Query(None, description="TV, MOVIE, OVA, ONA, SPECIAL, MUSIC"),
    status: Optional[str] = Query(None, description="RELEASING, FINISHED, NOT_YET_RELEASED, CANCELLED, HIATUS"),
    sort: str = Query("POPULARITY_DESC", description="Sort order"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
):
    """Advanced anime filter with genre, tag, year, season, format, status, and sort."""
    # Build dynamic argument string
    args = ["type: ANIME", f"sort: [{SORT_MAP.get(sort, 'POPULARITY_DESC')}]"]
    variables = {"page": page, "perPage": per_page}

    if genre:
        args.append("genre: $genre")
        variables["genre"] = genre
    if tag:
        args.append("tag: $tag")
        variables["tag"] = tag
    if year:
        args.append("seasonYear: $seasonYear")
        variables["seasonYear"] = year
    if season:
        args.append("season: $season")
        variables["season"] = season.upper()
    if format:
        args.append("format: $format")
        variables["format"] = format.upper()
    if status:
        args.append("status: $status")
        variables["status"] = status.upper()

    # Build variable type declarations
    var_types = ["$page: Int", "$perPage: Int"]
    if genre:
        var_types.append("$genre: String")
    if tag:
        var_types.append("$tag: String")
    if year:
        var_types.append("$seasonYear: Int")
    if season:
        var_types.append("$season: MediaSeason")
    if format:
        var_types.append("$format: MediaFormat")
    if status:
        var_types.append("$status: MediaStatus")

    gql = f"""
    query ({', '.join(var_types)}) {{
        Page(page: $page, perPage: $perPage) {{
            pageInfo {{ total currentPage lastPage hasNextPage perPage }}
            media({', '.join(args)}) {{
                {MEDIA_LIST_FIELDS}
            }}
        }}
    }}
    """
    data = await _anilist_query(gql, variables)
    page_data = data.get("Page", {})
    page_info = page_data.get("pageInfo", {})
    response = {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "results": page_data.get("media", []),
    }
    return _proxy_deep_images(response)


# ─── Collection Endpoints (with pagination) ─────────────────────────────────

async def _fetch_collection(sort_type: str, status: str = None, page: int = 1, per_page: int = 20):
    """Internal helper for fetching collections like trending, popular, etc."""
    status_filter = f", status: {status}" if status else ""
    gql = f"""
    query ($page: Int, $perPage: Int) {{
        Page(page: $page, perPage: $perPage) {{
            pageInfo {{ total currentPage lastPage hasNextPage perPage }}
            media(type: ANIME, sort: [{sort_type}]{status_filter}) {{
                {MEDIA_LIST_FIELDS}
            }}
        }}
    }}
    """
    data = await _anilist_query(gql, {"page": page, "perPage": per_page})
    page_data = data.get("Page", {})
    page_info = page_data.get("pageInfo", {})
    response = {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "results": page_data.get("media", []),
    }
    return _proxy_deep_images(response)


@app.get("/spotlight")
async def get_spotlight():
    """Get the spotlight anime – high-priority trending and popular titles."""
    gql = f"""
    query {{
        Page(page: 1, perPage: 10) {{
            media(sort: [TRENDING_DESC, POPULARITY_DESC], type: ANIME) {{
                {MEDIA_LIST_FIELDS}
            }}
        }}
    }}
    """
    data = await _anilist_query(gql)
    media = data.get("Page", {}).get("media", [])
    return _proxy_deep_images({"results": media})


@app.get("/trending")
async def get_trending(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
):
    """Get trending anime with full metadata and pagination."""
    return await _fetch_collection("TRENDING_DESC", page=page, per_page=per_page)


@app.get("/popular")
async def get_popular(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
):
    """Get most popular anime of all time with full metadata and pagination."""
    return await _fetch_collection("POPULARITY_DESC", page=page, per_page=per_page)


@app.get("/upcoming")
async def get_upcoming(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
):
    """Get upcoming anime with full metadata and pagination."""
    return await _fetch_collection("POPULARITY_DESC", "NOT_YET_RELEASED", page=page, per_page=per_page)


@app.get("/recent")
async def get_recent(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
):
    """Get currently airing anime with full metadata and pagination."""
    return await _fetch_collection("START_DATE_DESC", "RELEASING", page=page, per_page=per_page)

@app.get("/health")
async def health():
    """Basic liveness check — always returns 200 if the server is up. Bypasses auth."""
    return {"status": "ok"}


@app.post("/internal/notify")
async def internal_notify(payload: dict):
    """Telegram-alert relay for the 4 cloud nodes, which have no local Hermes install — see
    NOTIFY_RELAY_URL above. Protected by the normal x-api-key/origin check (not in the auth
    bypass list), so only requests carrying this deployment's own API_KEY get through."""
    message = payload.get("message")
    if not message:
        raise HTTPException(status_code=400, detail="message required")
    _notify_telegram(message)
    return {"status": "ok"}


@app.get("/cache-status")
async def get_cache_status():
    """Check whether Redis caching is configured and reachable."""
    if not REDIS_ENABLED:
        return {"enabled": False, "connected": False, "reason": "REDIS_HOST/REDIS_PORT not set"}

    try:
        await redis_client.ping()
        return {"enabled": True, "connected": True}
    except Exception as e:
        return {"enabled": True, "connected": False, "reason": str(e)}


@app.get("/recent-episodes-old")
async def get_recent_episodes_old():
    """Get currently airing anime with full metadata and pagination."""
    cached = await _cache_get(REDIS_KEY_RECENT_EPISODES)
    if cached:
        return cached

    recents = await _fetch_raw_recents()
    result = _proxy_deep_images(recents)
    await _cache_set(REDIS_KEY_RECENT_EPISODES, result, CACHE_RECENT_EPISODES_TTL)
    return result


def _recompute_time_until_airing(data: list) -> list:
    """Recompute each item's top-level timeUntilAiring from its (stable) airingAt against the
    current server time. Miruro's own value is a snapshot that can stay frozen for hours, which
    breaks clients that gate on a tight timeUntilAiring threshold (e.g. "already aired")."""
    now = int(time.time())
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("airingAt"), int):
            item["timeUntilAiring"] = item["airingAt"] - now
    return data


def _filter_tv_format(data: list) -> list:
    """Keep only TV-format entries. The only consumer of /recent-episodes discards every other
    format client-side anyway, so filtering here just saves payload size and parsing."""
    return [item for item in data if isinstance(item, dict) and item.get("media", {}).get("format") == "TV"]


def _fix_next_airing_episode(data: list) -> list:
    """Force media.nextAiringEpisode.episode to episode + 1. Miruro's schedule snapshot doesn't
    always advance nextAiringEpisode in sync with the real air time, so it sometimes still equals
    the item's own episode instead of episode + 1 — the client derives the displayed episode as
    nextAiringEpisode.episode - 1, so a non-advanced value makes it under-report by one."""
    for item in data:
        if not isinstance(item, dict):
            continue
        ep = item.get("episode")
        media = item.get("media")
        if not isinstance(ep, int) or not isinstance(media, dict):
            continue
        nae = media.get("nextAiringEpisode")
        if isinstance(nae, dict) and isinstance(nae.get("episode"), int):
            nae["episode"] = ep + 1
    return data


@app.get("/recent-episodes")
async def get_recent_episodes():
    """Get currently airing anime with full metadata and pagination."""
    cached = await _cache_get(REDIS_KEY_RECENT_EPISODES)
    if cached:
        return _fix_next_airing_episode(_recompute_time_until_airing(_filter_tv_format(cached)))

    recents = await _fetch_raw_recents()
    result = _proxy_deep_images(recents)
    await _cache_set(REDIS_KEY_RECENT_EPISODES, result, CACHE_RECENT_EPISODES_TTL)
    return _fix_next_airing_episode(_recompute_time_until_airing(_filter_tv_format(result)))

@app.get("/schedule")
async def get_schedule(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=50),
):
    """Get upcoming airing schedule with UNIX timestamps and full anime metadata."""
    gql = f"""
    query ($page: Int, $perPage: Int) {{
        Page(page: $page, perPage: $perPage) {{
            pageInfo {{ total currentPage lastPage hasNextPage perPage }}
            airingSchedules(notYetAired: true, sort: TIME) {{
                episode
                airingAt
                timeUntilAiring
                media {{
                    {MEDIA_LIST_FIELDS}
                }}
            }}
        }}
    }}
    """
    data = await _anilist_query(gql, {"page": page, "perPage": per_page})
    page_data = data.get("Page", {})
    page_info = page_data.get("pageInfo", {})
    results = []
    for item in page_data.get("airingSchedules", []):
        entry = item.get("media", {})
        entry["next_episode"] = item.get("episode")
        entry["airingAt"] = item.get("airingAt")
        entry["timeUntilAiring"] = item.get("timeUntilAiring")
        results.append(entry)
    response = {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "results": results,
    }
    return _proxy_deep_images(response)


# ─── Anime Details ───────────────────────────────────────────────────────────

@app.get("/info/{anilist_id}")
async def get_anime_info(anilist_id: int):
    """Get complete anime page data — everything AniList has to offer."""
    gql = f"""
    query ($id: Int) {{
        Media(id: $id, type: ANIME) {{
            {MEDIA_FULL_FIELDS}
        }}
    }}
    """
    data = await _anilist_query(gql, {"id": anilist_id})
    media = data.get("Media")
    if not media:
        raise HTTPException(status_code=404, detail="Anime not found")
    return _proxy_deep_images(media)


@app.get("/anime/{anilist_id}/characters")
async def get_anime_characters(
    anilist_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=50),
):
    """Get paginated character list with voice actors for an anime."""
    gql = """
    query ($id: Int, $page: Int, $perPage: Int) {
        Media(id: $id, type: ANIME) {
            id
            title { romaji english }
            characters(sort: [ROLE, RELEVANCE], page: $page, perPage: $perPage) {
                pageInfo { total currentPage lastPage hasNextPage perPage }
                edges {
                    role
                    node {
                        id
                        name { full native userPreferred }
                        image { large medium }
                        description
                        gender
                        dateOfBirth { year month day }
                        age
                        favourites
                        siteUrl
                    }
                    voiceActors {
                        id
                        name { full native }
                        image { large }
                        languageV2
                    }
                }
            }
        }
    }
    """
    data = await _anilist_query(gql, {"id": anilist_id, "page": page, "perPage": per_page})
    media = data.get("Media")
    if not media:
        raise HTTPException(status_code=404, detail="Anime not found")
    chars = media.get("characters", {})
    page_info = chars.get("pageInfo", {})
    response = {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "characters": chars.get("edges", []),
    }
    return _proxy_deep_images(response)


@app.get("/anime/{anilist_id}/relations")
async def get_anime_relations(anilist_id: int):
    """Get all related anime/manga for an anime (sequels, prequels, side stories, etc.)."""
    gql = """
    query ($id: Int) {
        Media(id: $id, type: ANIME) {
            id
            title { romaji english }
            relations {
                edges {
                    relationType(version: 2)
                    node {
                        id
                        title { romaji english native }
                        coverImage { large }
                        bannerImage
                        format
                        type
                        status
                        episodes
                        chapters
                        meanScore
                        averageScore
                        popularity
                        startDate { year month day }
                    }
                }
            }
        }
    }
    """
    data = await _anilist_query(gql, {"id": anilist_id})
    media = data.get("Media")
    if not media:
        raise HTTPException(status_code=404, detail="Anime not found")
    response = {
        "id": media["id"],
        "title": media["title"],
        "relations": media.get("relations", {}).get("edges", []),
    }
    return _proxy_deep_images(response)


@app.get("/anime/{anilist_id}/recommendations")
async def get_anime_recommendations(
    anilist_id: int,
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=25),
):
    """Get paginated community recommendations for an anime."""
    gql = """
    query ($id: Int, $page: Int, $perPage: Int) {
        Media(id: $id, type: ANIME) {
            id
            title { romaji english }
            recommendations(sort: RATING_DESC, page: $page, perPage: $perPage) {
                pageInfo { total currentPage lastPage hasNextPage perPage }
                nodes {
                    rating
                    mediaRecommendation {
                        id
                        title { romaji english native }
                        coverImage { large extraLarge }
                        bannerImage
                        format
                        episodes
                        status
                        meanScore
                        averageScore
                        popularity
                        genres
                        startDate { year }
                    }
                }
            }
        }
    }
    """
    data = await _anilist_query(gql, {"id": anilist_id, "page": page, "perPage": per_page})
    media = data.get("Media")
    if not media:
        raise HTTPException(status_code=404, detail="Anime not found")
    recs = media.get("recommendations", {})
    page_info = recs.get("pageInfo", {})
    response = {
        "page": page_info.get("currentPage", page),
        "perPage": page_info.get("perPage", per_page),
        "total": page_info.get("total", 0),
        "hasNextPage": page_info.get("hasNextPage", False),
        "recommendations": recs.get("nodes", []),
    }
    return _proxy_deep_images(response)


# ─── Streaming (Pipe-based — unchanged logic) ───────────────────────────────

@app.get("/episodes/{anilist_id}")
async def get_episodes(anilist_id: int):
    """Get the episode list for an anime, with slugified source IDs."""
    cache_key = f"{REDIS_KEY_EPISODES_PREFIX}:{anilist_id}"
    cached = await _cache_get(cache_key)
    if cached:
        return cached

    data = await _fetch_raw_episodes(anilist_id)
    data = _filter_blocked_prefixes(data)
    result = _proxy_deep_images(_inject_source_slugs(data, anilist_id))
    await _cache_set(cache_key, result, CACHE_EPISODES_TTL)
    return result


@app.get("/sources")
async def get_sources(
    episodeId: str = Query(..., description="Plain-text episode ID from /episodes response"),
    provider: str = Query(..., description="Provider name, e.g. kiwi, arc, telli"),
    anilistId: int = Query(..., description="AniList anime ID"),
    category: str = Query("sub", description="sub or dub"),
):
    """Get M3U8 streaming sources for a specific episode."""
    enc_id = base64.urlsafe_b64encode(episodeId.encode()).decode().rstrip('=')
    payload = {
        "path": "sources",
        "method": "GET",
        "query": {
            "episodeId": enc_id,
            "provider": provider,
            "category": category,
            "anilistId": anilistId,
        },
        "body": None,
        "version": "0.1.0",
    }
    encoded_req = _encode_pipe_request(payload)
    data = await _pipe_get(encoded_req)
    return _proxy_deep_images(data)

def _force_mp4_to_hls_temp(data: dict) -> dict:
    """TEMP (ver SESSION_LOG.md): fuerza streams con type=mp4 a hls. Borrar esta función
    completa y su única llamada en get_watch_sources para revertir. No toca "embed" ni "hls"."""
    for stream in data.get("streams", []):
        if stream.get("type") == "mp4":
            stream["type"] = "hls"
    return data

@app.get("/watch/{provider}/{anilist_id}/{category}/{slug}")
async def get_watch_sources(provider: str, anilist_id: int, category: str, slug: str):
    """The super simple sources endpoint resolving slugs (prefix-number) back to provider IDs."""
    data = await _fetch_raw_episodes(anilist_id)
    prov_data = data.get("providers", {}).get(provider, {})
    ep_list = prov_data.get("episodes", {}).get(category, [])

    # Resolve the slug back to the original ID
    target_id = None
    for ep in ep_list:
        orig_id = ep.get("id", "")
        prefix = orig_id.split(":")[0] if ":" in orig_id else orig_id
        generated = f"{prefix}-{ep.get('number')}"
        if generated == slug:
            target_id = orig_id
            break

    if not target_id:
        raise HTTPException(status_code=404, detail=f"Episode slug '{slug}' not found for provider {provider}")

    result = await get_sources(episodeId=target_id, provider=provider, anilistId=anilist_id, category=category)
    return _force_mp4_to_hls_temp(result)  # TEMP: quitar esta línea para revertir (ver SESSION_LOG.md)
