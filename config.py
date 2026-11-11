#!/usr/bin/env python3
"""
Configuration loader for Local Media Plex, Emby & Jellyfin VideoWall Selector.
Loads settings from environment variables, an optional .env file, and an optional config.json file
using only the Python standard library.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _load_dotenv(dotenv_path: Path) -> None:
    """Minimal zero-dependency .env loader (does not overwrite existing environment variables)."""
    if not dotenv_path.is_file():
        return
    try:
        for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


def parse_path_mappings(raw: str | dict | list | None) -> list[tuple[str, str]]:
    """
    Parse path prefix rewrites so FFmpeg can read media directly from a local/NAS mount
    when available, while falling back automatically to HTTP streaming when unmapped.

    Supported formats:
      - ENV string: "/mnt/media/Movies=\\\\nas\\Movies;/data/films=/Volumes/Films"
      - JSON dict:  {"/mnt/media/Movies": "\\\\nas\\Movies"}
    """
    mappings: list[tuple[str, str]] = []
    if not raw:
        return mappings

    if isinstance(raw, dict):
        for src_prefix, dst_prefix in raw.items():
            if src_prefix and dst_prefix:
                mappings.append((str(src_prefix), str(dst_prefix)))
        return mappings

    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and entry.get("from") and entry.get("to"):
                mappings.append((str(entry["from"]), str(entry["to"])))
        return mappings

    for part in str(raw).split(";"):
        part = part.strip()
        if "=" in part:
            src_prefix, dst_prefix = part.split("=", 1)
            src_prefix = src_prefix.strip()
            dst_prefix = dst_prefix.strip()
            if src_prefix and dst_prefix:
                mappings.append((src_prefix, dst_prefix))
    return mappings


def map_server_path(server_file_path: str, mappings: list[tuple[str, str]]) -> str:
    """Rewrite a media server file path using configured prefix mappings if matched."""
    if not server_file_path or not mappings:
        return ""
    norm_in = server_file_path.replace("\\", "/")
    for src_prefix, dst_prefix in mappings:
        norm_src = src_prefix.replace("\\", "/").rstrip("/")
        if norm_in == norm_src or norm_in.startswith(norm_src + "/"):
            rel = norm_in[len(norm_src):].lstrip("/")
            if "\\" in dst_prefix or (len(dst_prefix) >= 2 and dst_prefix[1] == ":"):
                rel_win = rel.replace("/", "\\")
                return dst_prefix.rstrip("\\") + ("\\" + rel_win if rel_win else "")
            return str(Path(dst_prefix) / rel)
    return ""


def find_ffmpeg() -> str:
    """Locate the FFmpeg executable via FFMPEG_BIN or system PATH."""
    candidates = [
        os.environ.get("FFMPEG_BIN", "").strip(),
        r"C:\ffmpeg\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    found = shutil.which("ffmpeg")
    if found:
        return found
    return "ffmpeg"


@dataclass
class ServerEntry:
    id: str
    provider: str  # "plex", "emby", or "jellyfin"
    url: str
    token: str
    name: str = ""
    section_id: str = ""
    user_id: str = ""
    machine_id: str = ""
    library_name: str = "Movies"
    path_mappings: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class AppConfig:
    wall_dir: Path
    host: str
    port: int
    kiosk_name: str
    audio_sink_match: str
    portrait_cols: int
    portrait_rows: int
    landscape_cols: int
    landscape_rows: int
    clip_duration_sec: int
    clip_seek_ratio: float
    tile_width: int
    tile_height: int
    auto_glide_idle_sec: int
    servers: list[ServerEntry] = field(default_factory=list)
    ha_url: str = ""
    ha_token: str = ""
    cast_targets: list[dict[str, Any]] = field(default_factory=list)


def load_config(project_root: Path | None = None) -> AppConfig:
    root = project_root or Path(__file__).resolve().parent
    _load_dotenv(root / ".env")

    cfg_file_env = os.environ.get("CONFIG_FILE", "").strip()
    cfg_path = Path(cfg_file_env) if cfg_file_env else (root / "config.json")
    file_data: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            file_data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[WARN] Could not parse {cfg_path}: {exc}", flush=True)

    def _get(env_key: str, json_key: str, default: Any) -> Any:
        env_val = os.environ.get(env_key)
        if env_val is not None and env_val != "":
            return env_val
        if json_key in file_data and file_data[json_key] is not None:
            return file_data[json_key]
        return default

    wall_dir_raw = str(_get("WALL_DIR", "wall_dir", str(root / "wall_data")))
    wall_dir = Path(wall_dir_raw).expanduser()
    if not wall_dir.is_absolute():
        wall_dir = (root / wall_dir).resolve()

    default_mappings = parse_path_mappings(_get("MEDIA_PATH_MAP", "media_path_map", ""))

    servers: list[ServerEntry] = []
    raw_servers = file_data.get("servers")
    if isinstance(raw_servers, list) and raw_servers:
        for idx, srv in enumerate(raw_servers):
            if not isinstance(srv, dict):
                continue
            prov = str(srv.get("provider") or "plex").strip().lower()
            url = str(srv.get("url") or "").strip().rstrip("/")
            token = str(srv.get("token") or "").strip()
            if not url:
                continue
            sid = str(srv.get("id") or f"{prov}{idx + 1}").strip()
            srv_mappings = parse_path_mappings(srv.get("path_map")) or default_mappings
            servers.append(
                ServerEntry(
                    id=sid,
                    provider=prov,
                    url=url,
                    token=token,
                    name=str(srv.get("name") or prov.capitalize()),
                    section_id=str(srv.get("section_id") or ""),
                    user_id=str(srv.get("user_id") or ""),
                    machine_id=str(srv.get("machine_id") or ""),
                    library_name=str(srv.get("library_name") or "Movies"),
                    path_mappings=srv_mappings,
                )
            )
    else:
        provider = str(_get("MEDIA_PROVIDER", "media_provider", "plex")).strip().lower()
        media_url = str(_get("MEDIA_URL", "media_url", "")).strip().rstrip("/")
        media_token = str(_get("MEDIA_TOKEN", "media_token", "")).strip()

        # Also accept provider-specific aliases (PLEX_URL/PLEX_TOKEN or EMBY_URL/EMBY_TOKEN)
        if not media_url:
            if provider == "plex":
                media_url = os.environ.get("PLEX_URL", os.environ.get("PLEX_HOST", "")).strip().rstrip("/")
            else:
                media_url = os.environ.get("EMBY_URL", os.environ.get("JELLYFIN_URL", "")).strip().rstrip("/")
        if not media_token:
            if provider == "plex":
                media_token = os.environ.get("PLEX_TOKEN", "").strip()
            else:
                media_token = os.environ.get("EMBY_TOKEN", os.environ.get("JELLYFIN_TOKEN", "")).strip()

        if media_url:
            servers.append(
                ServerEntry(
                    id=provider,
                    provider=provider,
                    url=media_url,
                    token=media_token,
                    name=str(_get("MEDIA_SERVER_NAME", "media_server_name", provider.capitalize())),
                    section_id=str(_get("MEDIA_SECTION_ID", "media_section_id", "")),
                    user_id=str(_get("MEDIA_USER_ID", "media_user_id", "")),
                    machine_id=str(_get("PLEX_MACHINE_ID", "plex_machine_id", "")),
                    library_name=str(_get("MEDIA_LIBRARY_NAME", "media_library_name", "Movies")),
                    path_mappings=default_mappings,
                )
            )

    # Optional secondary server via env (e.g. household running BOTH Plex and Emby/Jellyfin simultaneously)
    secondary_url = os.environ.get("SECONDARY_MEDIA_URL", "").strip().rstrip("/")
    secondary_provider = os.environ.get("SECONDARY_MEDIA_PROVIDER", "").strip().lower()
    secondary_token = os.environ.get("SECONDARY_MEDIA_TOKEN", "").strip()
    if secondary_url and secondary_provider:
        servers.append(
            ServerEntry(
                id=f"{secondary_provider}2",
                provider=secondary_provider,
                url=secondary_url,
                token=secondary_token,
                name=secondary_provider.capitalize(),
                section_id=os.environ.get("SECONDARY_MEDIA_SECTION_ID", "").strip(),
                user_id=os.environ.get("SECONDARY_MEDIA_USER_ID", "").strip(),
                path_mappings=default_mappings,
            )
        )

    cast_targets_raw = file_data.get("cast_targets")
    if not isinstance(cast_targets_raw, list):
        env_targets = os.environ.get("CAST_TARGETS_JSON", "").strip()
        if env_targets:
            try:
                cast_targets_raw = json.loads(env_targets)
            except Exception:
                cast_targets_raw = []
        else:
            cast_targets_raw = []

    return AppConfig(
        wall_dir=wall_dir,
        host=str(_get("HOST", "host", "0.0.0.0")),
        port=int(_get("PORT", "port", 18500)),
        kiosk_name=str(_get("KIOSK_NAME", "kiosk_name", "This Screen")),
        audio_sink_match=str(_get("AUDIO_SINK_MATCH", "audio_sink_match", "")),
        portrait_cols=max(1, int(_get("PORTRAIT_COLS", "portrait_cols", 5))),
        portrait_rows=max(1, int(_get("PORTRAIT_ROWS", "portrait_rows", 16))),
        landscape_cols=max(1, int(_get("LANDSCAPE_COLS", "landscape_cols", 8))),
        landscape_rows=max(1, int(_get("LANDSCAPE_ROWS", "landscape_rows", 9))),
        clip_duration_sec=max(10, int(_get("CLIP_DURATION_SEC", "clip_duration_sec", 40))),
        clip_seek_ratio=min(0.85, max(0.05, float(_get("CLIP_SEEK_RATIO", "clip_seek_ratio", 0.35)))),
        tile_width=max(120, int(_get("TILE_WIDTH", "tile_width", 216))),
        tile_height=max(68, int(_get("TILE_HEIGHT", "tile_height", 120))),
        auto_glide_idle_sec=max(0, int(_get("AUTO_GLIDE_IDLE_SEC", "auto_glide_idle_sec", 75))),
        servers=servers,
        ha_url=str(_get("HA_URL", "ha_url", "")).strip().rstrip("/"),
        ha_token=str(_get("HA_TOKEN", "ha_token", "")).strip(),
        cast_targets=cast_targets_raw,
    )
