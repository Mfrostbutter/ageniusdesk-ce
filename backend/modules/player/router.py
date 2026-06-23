"""Spotify OAuth + Web API proxy routes."""

import base64
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from backend.config import decrypt_value, encrypt_value, load_config, save_config


def _build_redirect_uri(request: Request) -> str:
    """Build the Spotify redirect URI, substituting 127.0.0.1 for localhost
    (Spotify's new policy rejects 'localhost' as of April 2025)."""
    base = str(request.base_url).rstrip("/")
    # Normalize localhost → 127.0.0.1 for Spotify compliance
    base = base.replace("://localhost:", "://127.0.0.1:")
    base = base.replace("://localhost/", "://127.0.0.1/")
    if base.endswith("://localhost"):
        base = base.replace("://localhost", "://127.0.0.1")
    return base + "/api/spotify/callback"

router = APIRouter(prefix="/api/spotify", tags=["spotify"])

SPOTIFY_AUTH_URL  = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_URL   = "https://api.spotify.com/v1"
SCOPES = " ".join([
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
    "user-library-read",
    "user-library-modify",
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-read-recently-played",
    "user-read-email",
    "user-read-private",
])

# Temporary state store (in-memory, single-user dashboard)
_oauth_state: dict = {}


# ── Config helpers ─────────────────────────────────────────────────────────────

def get_spotify_config() -> dict:
    config = load_config()
    sp = config.get("spotify", {})
    return {
        "client_id":     sp.get("client_id", ""),
        "client_secret": decrypt_value(sp.get("client_secret", "")),
        "access_token":  decrypt_value(sp.get("access_token", "")),
        "refresh_token": decrypt_value(sp.get("refresh_token", "")),
        "token_expiry":  sp.get("token_expiry", 0),
        "display_name":  sp.get("display_name", ""),
    }


def save_spotify_tokens(access_token: str, refresh_token: str, expires_in: int, display_name: str = ""):
    config = load_config()
    sp = config.get("spotify", {})
    sp["access_token"]  = encrypt_value(access_token)
    sp["refresh_token"] = encrypt_value(refresh_token) if refresh_token else sp.get("refresh_token", "")
    sp["token_expiry"]  = int(time.time()) + expires_in - 60  # 60s buffer
    if display_name:
        sp["display_name"] = display_name
    config["spotify"] = sp
    save_config(config)


async def get_valid_token() -> str:
    """Return a valid access token, refreshing if needed."""
    cfg = get_spotify_config()
    if not cfg["refresh_token"]:
        raise HTTPException(status_code=401, detail="Spotify not connected")

    # Refresh if expired
    if time.time() >= cfg["token_expiry"]:
        cfg = await _refresh_token(cfg)

    return cfg["access_token"]


async def _refresh_token(cfg: dict) -> dict:
    auth = base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            SPOTIFY_TOKEN_URL,
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": cfg["refresh_token"]},
        )
        r.raise_for_status()
        data = r.json()
    save_spotify_tokens(
        data["access_token"],
        data.get("refresh_token", ""),  # Spotify sometimes issues a new refresh token
        data.get("expires_in", 3600),
    )
    return get_spotify_config()


# ── Auth endpoints ──────────────────────────────────────────────────────────────

class SpotifySetup(BaseModel):
    client_id: str
    client_secret: str


@router.post("/setup")
async def setup_spotify(req: SpotifySetup):
    """Save Spotify app credentials."""
    config = load_config()
    sp = config.get("spotify", {})
    sp["client_id"]     = req.client_id
    sp["client_secret"] = (
        encrypt_value(req.client_secret)
        if req.client_secret and not req.client_secret.startswith("$")
        else req.client_secret
    )
    config["spotify"] = sp
    save_config(config)
    return {"success": True}


@router.get("/auth")
async def start_auth(request: Request):
    """Redirect user to Spotify OAuth consent page."""
    cfg = get_spotify_config()
    if not cfg["client_id"]:
        raise HTTPException(status_code=400, detail="Spotify Client ID not configured. Go to Settings → Music Player.")

    state = secrets.token_urlsafe(16)
    _oauth_state["state"] = state

    redirect_uri = _build_redirect_uri(request)
    params = {
        "client_id":     cfg["client_id"],
        "response_type": "code",
        "redirect_uri":  redirect_uri,
        "scope":         SCOPES,
        "state":         state,
        "show_dialog":   "false",
    }
    return RedirectResponse(f"{SPOTIFY_AUTH_URL}?{urlencode(params)}")


@router.get("/callback")
async def oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Handle Spotify OAuth callback."""
    if error:
        return RedirectResponse("/?spotify_error=" + error)

    if state != _oauth_state.get("state", ""):
        return RedirectResponse("/?spotify_error=state_mismatch")

    cfg = get_spotify_config()
    redirect_uri = _build_redirect_uri(request)
    auth = base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            SPOTIFY_TOKEN_URL,
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
        )
        if r.status_code != 200:
            return RedirectResponse("/?spotify_error=token_exchange_failed")
        data = r.json()

        # Fetch display name inside the same client context
        display_name = ""
        try:
            mr = await client.get(
                f"{SPOTIFY_API_URL}/me",
                headers={"Authorization": f"Bearer {data['access_token']}"},
            )
            display_name = mr.json().get("display_name", "")
        except Exception:
            pass

    save_spotify_tokens(data["access_token"], data["refresh_token"], data.get("expires_in", 3600), display_name)
    return RedirectResponse("/?spotify_connected=1")


@router.post("/disconnect")
async def disconnect():
    """Remove Spotify tokens."""
    config = load_config()
    config["spotify"] = {
        "client_id":     config.get("spotify", {}).get("client_id", ""),
        "client_secret": config.get("spotify", {}).get("client_secret", ""),
    }
    save_config(config)
    return {"success": True}


@router.get("/status")
async def status():
    """Check Spotify connection status."""
    cfg = get_spotify_config()
    connected = bool(cfg["refresh_token"])
    return {
        "connected": connected,
        "display_name": cfg["display_name"],
        "has_credentials": bool(cfg["client_id"] and cfg["client_secret"]),
        "client_id": cfg["client_id"],
    }


# ── Playback endpoints ──────────────────────────────────────────────────────────

@router.get("/current")
async def get_current():
    """Get currently playing track."""
    token = await get_valid_token()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{SPOTIFY_API_URL}/me/player/currently-playing",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code == 204:
            return {"playing": False, "track": None}
        if r.status_code != 200:
            return {"playing": False, "track": None}
        return {"playing": True, "data": r.json()}


@router.get("/player")
async def get_player_state():
    """Get full player state (device, shuffle, repeat, volume)."""
    token = await get_valid_token()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{SPOTIFY_API_URL}/me/player",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code == 204:
            return {"active": False}
        if r.status_code == 403:
            return {
                "active": False,
                "error": "premium_required",
                "message": (
                    "Spotify Premium required for playback "
                    "control, or add your account to the app's "
                    "User Management in Development mode."
                ),
            }
        if not r.is_success:
            return {"active": False, "error": f"spotify_{r.status_code}"}
        return {"active": True, "data": r.json()}


@router.post("/play")
async def play():
    """Resume playback."""
    token = await get_valid_token()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.put(
            f"{SPOTIFY_API_URL}/me/player/play",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code in (204, 200):
            return {"success": True}
        raise HTTPException(status_code=r.status_code, detail=r.text[:200])


@router.post("/pause")
async def pause():
    """Pause playback."""
    token = await get_valid_token()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.put(
            f"{SPOTIFY_API_URL}/me/player/pause",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code in (204, 200):
            return {"success": True}
        raise HTTPException(status_code=r.status_code, detail=r.text[:200])


@router.post("/next")
async def next_track():
    """Skip to next track."""
    token = await get_valid_token()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{SPOTIFY_API_URL}/me/player/next",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code in (204, 200):
            return {"success": True}
        raise HTTPException(status_code=r.status_code, detail=r.text[:200])


@router.post("/previous")
async def previous_track():
    """Skip to previous track."""
    token = await get_valid_token()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{SPOTIFY_API_URL}/me/player/previous",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code in (204, 200):
            return {"success": True}
        raise HTTPException(status_code=r.status_code, detail=r.text[:200])


@router.post("/shuffle/{state}")
async def set_shuffle(state: str):
    """Toggle shuffle on/off."""
    token = await get_valid_token()
    val = "true" if state.lower() in ("true", "1", "on") else "false"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.put(
            f"{SPOTIFY_API_URL}/me/player/shuffle?state={val}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code in (204, 200):
            return {"success": True}
        raise HTTPException(status_code=r.status_code, detail=r.text[:200])


@router.post("/volume/{level}")
async def set_volume(level: int):
    """Set volume 0-100."""
    if not 0 <= level <= 100:
        raise HTTPException(status_code=400, detail="Volume must be 0-100")
    token = await get_valid_token()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.put(
            f"{SPOTIFY_API_URL}/me/player/volume?volume_percent={level}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code in (204, 200):
            return {"success": True}
        raise HTTPException(status_code=r.status_code, detail=r.text[:200])


@router.post("/seek/{position_ms}")
async def seek(position_ms: int):
    """Seek to position in ms."""
    token = await get_valid_token()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.put(
            f"{SPOTIFY_API_URL}/me/player/seek?position_ms={position_ms}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code in (204, 200):
            return {"success": True}
        raise HTTPException(status_code=r.status_code, detail=r.text[:200])


# ── Library endpoints ───────────────────────────────────────────────────────────

@router.get("/playlists")
async def get_playlists(limit: int = 20):
    """Get user's playlists."""
    token = await get_valid_token()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{SPOTIFY_API_URL}/me/playlists?limit={min(limit, 50)}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code == 403:
            return {"items": [], "error": "premium_required"}
        if not r.is_success:
            return {"items": [], "error": f"spotify_{r.status_code}"}
        return r.json()


@router.post("/play/playlist/{playlist_id}")
async def play_playlist(playlist_id: str):
    """Start playing a playlist."""
    token = await get_valid_token()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.put(
            f"{SPOTIFY_API_URL}/me/player/play",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"context_uri": f"spotify:playlist:{playlist_id}"},
        )
        if r.status_code in (204, 200):
            return {"success": True}
        raise HTTPException(status_code=r.status_code, detail=r.text[:200])


@router.get("/search")
async def search(q: str, type: str = "track,playlist", limit: int = 10):
    """Search Spotify catalog."""
    token = await get_valid_token()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{SPOTIFY_API_URL}/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": q, "type": type, "limit": min(limit, 20)},
        )
        if r.status_code == 403:
            return {"error": "premium_required", "tracks": {"items": []}, "playlists": {"items": []}}
        if not r.is_success:
            return {"error": f"spotify_{r.status_code}", "tracks": {"items": []}, "playlists": {"items": []}}
        return r.json()


@router.post("/play/track/{track_id}")
async def play_track(track_id: str):
    """Play a specific track."""
    token = await get_valid_token()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.put(
            f"{SPOTIFY_API_URL}/me/player/play",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"uris": [f"spotify:track:{track_id}"]},
        )
        if r.status_code in (204, 200):
            return {"success": True}
        raise HTTPException(status_code=r.status_code, detail=r.text[:200])


@router.get("/devices")
async def get_devices():
    """Get available Spotify Connect devices."""
    token = await get_valid_token()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{SPOTIFY_API_URL}/me/player/devices",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code == 403:
            return {"devices": [], "error": "premium_required"}
        if not r.is_success:
            return {"devices": [], "error": f"spotify_{r.status_code}"}
        return r.json()
