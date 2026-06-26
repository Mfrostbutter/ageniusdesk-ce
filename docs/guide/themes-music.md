# Themes & Music

Two cosmetic-but-useful features. **Themes** recolor the whole dashboard by overriding CSS custom properties from a theme JSON; you switch themes from Settings, and the API can store custom themes. **Music** ("Your Vibe") is a built-in player that embeds external audio sources (Bandcamp, Mixcloud, self-hosted servers, internet radio, Spotify) and can be driven by n8n workflows. Neither feature touches your n8n data. The active theme id also shows in the [Admin](admin-users.md) System tab.

---

## Themes

A theme is a JSON document with a name and color map. Applying it sets `--<key>` CSS variables on the document root, optionally swaps body/mono fonts, and can toggle a matrix-rain effect.

### Built-in themes

AgeniusDesk CE ships two built-in themes:

| Theme id | Name |
|---|---|
| `dark` | Dark (default) |
| `default-light` | Light |

Built-in themes live in `frontend/themes/`. Custom themes are stored under `data/themes/`. Both are returned together by `GET /api/themes`.

### Switch the theme

1. Open **Settings** and select the **Themes** tab.
2. The Theme card shows a grid of theme cards, each with up to four color swatches and the theme name. The active theme is highlighted.
3. Click a theme card to apply it.

Applying calls `POST /api/themes/active/{theme_id}` (which validates the id exists and persists it to config), then re-fetches and applies the theme JSON live. No reload needed.

### Theme JSON shape

```json
{
  "name": "My Theme",
  "author": "you",
  "version": "1.0",
  "colors": { "bg-void": "#0b0b0f", "accent": "#ff6d5a", "text-primary": "#f5f5f7" },
  "fonts": { "body": "Inter", "mono": "JetBrains Mono" },
  "effects": { "matrix-rain": false }
}
```

- `colors` keys become `--<key>` CSS variables, so they must match the variable names the dashboard CSS uses (see `frontend/css/base.css`).
- `fonts.body` and `fonts.mono` set `--font-body` and `--font-mono`.
- `effects["matrix-rain"]: true` turns on the animated background; any other value turns it off.

### Save a custom theme via the API

The current Settings UI only switches between existing themes; it does not include a custom-theme editor. To add a custom theme, POST its JSON to the API:

1. `POST /api/themes` with a body matching the shape above (`name` and `colors` are required; `author`, `version`, `fonts`, `effects` are optional).
2. The server derives the theme id from the name: lowercased, every run of non-`[A-Za-z0-9_-]` characters collapsed to `-`, leading/trailing `-` trimmed, capped at 64 characters. A name with no letters or numbers is rejected with a 400.
3. The theme is written to `data/themes/<id>.json` and immediately appears in the Settings theme grid.
4. Apply it like any other theme (click it, or `POST /api/themes/active/<id>`).

> Theme id safety: ids must match `^[A-Za-z0-9_-]{1,64}$`, and the resolved file path is asserted to stay inside the themes directory, so a crafted id cannot write or read outside `data/themes/` or `frontend/themes/`.

---

## Music ("Your Vibe")

A persistent dashboard music player. Open it from the **Your Vibe** view. State (embeds, vibes, history, preferences, triggers) is backend-synced under `/api/music/*`, and the floating player component refreshes live on the `music:config-changed` event.

The view is organized into tabs.

### Sources

Add custom embeds. Paste an `<iframe>` snippet or a direct URL from any service; input is sanitized so only safe iframe attributes survive.

1. Enter a **Name**, optional **Icon** and **color**.
2. Paste an iframe or URL into **Embed HTML or URL**.
3. **Preview** sanitizes and renders it, reporting the detected host (and whether it is a known host).
4. **Save embed** stores it; saved embeds appear below with play (`▶`) and delete (`✕`) buttons.

Built-in **Templates** pre-fill the form with a working example for common services:

| Template | Source type |
|---|---|
| Bandcamp | Album/track embed iframe |
| Mixcloud | Mix embed iframe |
| Radio Garden | Station URL |
| Navidrome | Self-hosted server URL |
| Jellyfin | Self-hosted web URL (needs CSP that allows embedding) |
| Plex | Plex Web URL (may be blocked by X-Frame-Options) |
| Last.fm | Artist/track page URL (no official embed) |
| Internet Radio | Direct stream URL (.mp3/.aac/.ogg, icecast/shoutcast) |

Whether a given source actually plays in an iframe depends on that service's embedding policy (CSP / X-Frame-Options); some self-hosted and major services block framing.

### Library

Playback history, capped (default 100; pinned items never count against the cap). Search by URL/title, filter pinned/unpinned, play, pin, tag, or delete entries, clear unpinned, and export history as JSON.

### Vibes

A vibe is a named bundle of URLs. Give it a name, optional description, icon, color, and one URL per line. **Launch** queues all its URLs into the player.

### Appearance

Tune the player's look: EQ visualizer (on/off, style, bar count), banner height/position, album art and progress/controls toggles, and an accent-color override (or reset to the theme accent).

### Behavior

Playback preferences: autoplay on paste, auto-advance, persist across reloads, auto-pause on an n8n workflow error, a default service launcher, a global toggle hotkey, and the history cap. A reset button restores appearance + behavior defaults.

### n8n Triggers

Let n8n workflows control the player.

1. Toggle **Enable trigger webhook**.
2. Copy the **Webhook URL** (`/api/music/triggers/fire`) and the **Auth token** (rotatable).
3. From an n8n HTTP Request node, POST to the webhook with the token as `Authorization: Bearer <token>` or `X-Vibe-Token: <token>`, and a body like:

   ```json
   { "action": "play", "url": "https://open.spotify.com/track/xxxx",
     "workflow_id": "{{ $workflow.id }}", "instance_id": "prod" }
   ```

   Actions: `play`, `pause`, `next`, `prev`, `stop`.
4. **Default reactions** let you auto-pause/play/stop on any workflow error or success (driven by the error-handler stream). See [Errors](errors.md) for the error feed.

### Spotify

Optional full-control integration (album art, search, playlists, skip, seek, volume). Requires a Spotify app (Client ID/Secret) and Spotify Premium for playback control. The tab shows the exact **Redirect URI** to register in your Spotify app dashboard; note it uses `127.0.0.1` rather than `localhost` per Spotify's policy. Connect, disconnect, and check active devices from here.

### Data

Export all Your Vibe data (history + vibes + embeds) as one JSON file for backup or moving instances, plus a danger zone to clear unpinned history and reset appearance/behavior.
