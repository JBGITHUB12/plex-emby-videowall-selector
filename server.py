#!/usr/bin/env python3
"""
Local Media Plex, Emby & Jellyfin VideoWall Selector — HTTP, Byte-Range & LAN Cast Server
Serves the zero-border live VideoWall UI (Portrait & Landscape), streams 120p scene clips via
HTTP/1.1 206 Partial Content, provides instant full-movie local playback ("Play Here" with live
FFmpeg audio remuxing for DTS/TrueHD/AC3 MKVs), and dispatches movies with 1 tap to any
Plex, Emby, Jellyfin, or Home Assistant / Cast player on your local network.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from config import AppConfig, find_ffmpeg, load_config
from media_backends import MediaRouter

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class MovieCatalogCache:
    """Caches ready MP4 clips on disk every 3 seconds to avoid per-request directory scans."""

    def __init__(self, wall_dir: Path, cfg: AppConfig) -> None:
        self.wall_dir = wall_dir
        self.cfg = cfg
        self.clips_dir = wall_dir / "clips"
        self.catalog_path = wall_dir / "catalog.json"
        self.encoder_status_path = wall_dir / "encoder_status.json"
        self._lock = threading.Lock()
        self._last_scan = 0.0
        self._payload_bytes = b'{"total_movies":0,"ready_count":0,"movies":[]}'
        self._status: dict[str, Any] = {"total_movies": 0, "ready_count": 0, "percent": 0.0}

    def get_movies_json(self) -> bytes:
        now = time.monotonic()
        if now - self._last_scan < 3.0:
            return self._payload_bytes
        with self._lock:
            if time.monotonic() - self._last_scan < 3.0:
                return self._payload_bytes
            self._refresh()
            return self._payload_bytes

    def get_status(self) -> dict[str, Any]:
        self.get_movies_json()
        st = dict(self._status)
        if self.encoder_status_path.is_file():
            try:
                enc = json.loads(self.encoder_status_path.read_text(encoding="utf-8"))
                st["encoding_active"] = bool(enc.get("encoding_active", False))
                st["clips_per_sec"] = enc.get("clips_per_sec", 0.0)
            except Exception:
                pass
        return st

    def _refresh(self) -> None:
        ready_stems: set[str] = set()
        if self.clips_dir.exists():
            with os.scandir(self.clips_dir) as it:
                for entry in it:
                    name = entry.name
                    if name.endswith(".mp4") and not name.endswith(".tmp.mp4"):
                        try:
                            if entry.stat().st_size > 8192:
                                ready_stems.add(name[:-4])
                        except OSError:
                            pass

        total_movies = len(ready_stems)
        collections_count = 0
        ordered_movies: list[dict[str, Any]] = []

        if self.catalog_path.exists():
            try:
                cat = json.loads(self.catalog_path.read_text(encoding="utf-8"))
                total_movies = int(
                    cat.get("total_movies")
                    or cat.get("total_plex")
                    or len(cat.get("movies") or [])
                )
                collections_count = int(cat.get("collections_count") or 0)
                for m in cat.get("movies", []):
                    k = str(m.get("k", ""))
                    if k in ready_stems:
                        ordered_movies.append(m)
            except Exception:
                ordered_movies = [{"k": k, "t": f"Movie {k}", "y": None} for k in sorted(ready_stems)]
        else:
            ordered_movies = [{"k": k, "t": f"Movie {k}", "y": None} for k in sorted(ready_stems)]

        payload = {
            "total_movies": total_movies,
            "collections_count": collections_count,
            "ready_count": len(ordered_movies),
            "movies": ordered_movies,
        }
        self._payload_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self._status = {
            "total_movies": total_movies,
            "collections_count": collections_count,
            "ready_count": len(ordered_movies),
            "percent": round((len(ordered_movies) / total_movies * 100.0), 1) if total_movies else 0.0,
            "configured_servers": [
                {"provider": s.provider, "name": s.name or s.provider.capitalize()}
                for s in self.cfg.servers
            ],
        }
        self._last_scan = time.monotonic()


class VideoWallHandler(BaseHTTPRequestHandler):
    wall_dir: Path
    static_dir: Path
    cfg: AppConfig
    router: MediaRouter
    cache: MovieCatalogCache

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_HEAD(self) -> None:
        self._serve(send_body=False)

    def do_GET(self) -> None:
        self._serve(send_body=True)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/cast":
            try:
                clen = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(clen) if clen > 0 else b"{}"
                body = json.loads(raw.decode("utf-8"))
                clip_key = str(body.get("key") or body.get("ratingKey") or "").strip()
                target_id = str(body.get("target_id") or "").strip()
                offset_sec = int(body.get("offset_sec") or 0)
                if not clip_key or not target_id:
                    self._send_json(
                        {"ok": False, "message": "Missing movie key or target_id"},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                res = self.router.cast_movie(clip_key, target_id, offset_sec)
                self._send_json(res, HTTPStatus.OK if res.get("ok") else HTTPStatus.BAD_GATEWAY)
            except Exception as exc:
                self._send_json({"ok": False, "message": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def _serve(self, send_body: bool) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parse_qs(parsed.query)

        try:
            if route in ("/", "/index.html"):
                self._serve_file(self.static_dir / "index.html", "text/html; charset=utf-8", send_body, cache_sec=0)
                return

            if route == "/api/config":
                providers = [s.provider for s in self.cfg.servers]
                ui_cfg = {
                    "kiosk_name": self.cfg.kiosk_name,
                    "audio_sink_match": self.cfg.audio_sink_match,
                    "portrait_cols": self.cfg.portrait_cols,
                    "portrait_rows": self.cfg.portrait_rows,
                    "landscape_cols": self.cfg.landscape_cols,
                    "landscape_rows": self.cfg.landscape_rows,
                    "auto_glide_idle_sec": self.cfg.auto_glide_idle_sec,
                    "providers": providers,
                }
                self._send_bytes(
                    json.dumps(ui_cfg, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                    send_body,
                )
                return

            if route == "/api/movies":
                body = self.cache.get_movies_json()
                self._send_bytes(body, "application/json; charset=utf-8", send_body)
                return

            if route == "/api/status":
                body = json.dumps(self.cache.get_status(), ensure_ascii=False).encode("utf-8")
                self._send_bytes(body, "application/json; charset=utf-8", send_body)
                return

            if route == "/api/devices":
                devs = self.router.discover_players()
                self._send_bytes(
                    json.dumps({"devices": devs}, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                    send_body,
                )
                return

            if route.startswith("/api/movie/"):
                clip_key = route.split("/api/movie/", 1)[1].strip("/")
                info = self.router.get_movie_details(clip_key)
                self._send_bytes(
                    json.dumps(info, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                    send_body,
                )
                return

            if route.startswith("/api/poster/") or route.startswith("/api/art/"):
                kind = "poster" if route.startswith("/api/poster/") else "art"
                clip_key = route.split("/")[-1]
                img_bytes, ctype = self.router.get_image_bytes(clip_key, kind)
                self._send_bytes(img_bytes, ctype, send_body, cache_sec=86400)
                return

            if route.startswith("/api/remux/"):
                clip_key = route.split("/api/remux/", 1)[1].strip("/")
                start_sec = max(0, int((qs.get("start") or ["0"])[0]))
                self._stream_live_remux(clip_key, start_sec, send_body)
                return

            if route.startswith("/clips/"):
                fname = Path(route).name
                if not fname.endswith(".mp4") or ".." in fname or fname.endswith(".tmp.mp4"):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                clip_path = self.wall_dir / "clips" / fname
                self._serve_file(clip_path, "video/mp4", send_body, cache_sec=86400)
                return

            rel = route.lstrip("/")
            candidate = (self.static_dir / rel).resolve()
            if str(candidate).startswith(str(self.static_dir.resolve())) and candidate.is_file():
                ctype = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
                self._serve_file(candidate, ctype, send_body, cache_sec=300)
                return

            self.send_error(HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            try:
                self._send_json({"ok": False, "message": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            except Exception:
                pass

    def _stream_live_remux(self, clip_key: str, start_sec: int, send_body: bool) -> None:
        movie = self.router.get_movie_details(clip_key)
        src = str(movie.get("local_path") or "")
        if not src or not os.path.exists(src):
            src = str(movie.get("direct_stream") or "")
        if not src:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not send_body:
            return

        ffmpeg_bin = find_ffmpeg()
        vcodec = str(movie.get("video_codec") or "h264").lower()
        v_args = (
            ["-c:v", "copy"]
            if vcodec in ("h264", "hevc", "av1")
            else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]
        )

        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(start_sec),
            "-i",
            src,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            *v_args,
            "-ac",
            "2",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            "pipe:1",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        finally:
            proc.kill()
            proc.wait()

    def _send_json(self, obj: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, content_type: str, send_body: bool, cache_sec: int = 0) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", f"public, max-age={cache_sec}" if cache_sec else "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def _serve_file(self, path: Path, content_type: str, send_body: bool, cache_sec: int = 3600) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        file_size = path.stat().st_size
        range_hdr = self.headers.get("Range")

        if range_hdr:
            m = RANGE_RE.match(range_hdr.strip())
            if m:
                start_s, end_s = m.groups()
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else file_size - 1
                if start >= file_size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, file_size - 1)
                length = end - start + 1

                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Type", content_type)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", f"public, max-age={cache_sec}" if cache_sec else "no-store")
                self.end_headers()

                if send_body:
                    with path.open("rb") as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk = f.read(min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(file_size))
        self.send_header("Cache-Control", f"public, max-age={cache_sec}" if cache_sec else "no-store")
        self.end_headers()

        if send_body:
            with path.open("rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)


class FastWallHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256


def main() -> None:
    cfg = load_config()
    parser = argparse.ArgumentParser(
        description="Local Media Plex, Emby & Jellyfin VideoWall Selector Server"
    )
    parser.add_argument("--wall-dir", default=str(cfg.wall_dir), help="Directory containing clips/ and catalog.json")
    parser.add_argument("--static-dir", default=str(Path(__file__).resolve().parent / "static"))
    parser.add_argument("--host", default=cfg.host)
    parser.add_argument("--port", type=int, default=cfg.port)
    args = parser.parse_args()

    wall_dir = Path(args.wall_dir).resolve()
    wall_dir.mkdir(parents=True, exist_ok=True)
    (wall_dir / "clips").mkdir(parents=True, exist_ok=True)

    VideoWallHandler.wall_dir = wall_dir
    VideoWallHandler.static_dir = Path(args.static_dir).resolve()
    VideoWallHandler.cfg = cfg
    VideoWallHandler.router = MediaRouter(cfg)
    VideoWallHandler.cache = MovieCatalogCache(wall_dir, cfg)

    srv = FastWallHTTPServer((args.host, args.port), VideoWallHandler)
    providers = ", ".join(s.provider.upper() for s in cfg.servers) or "UNCONFIGURED (.env)"
    print(
        f"[VideoWall] Serving Local Media VideoWall Selector on http://{args.host}:{args.port} "
        f"| Providers: {providers} | Data: {wall_dir}",
        flush=True,
    )
    srv.serve_forever()


if __name__ == "__main__":
    main()
