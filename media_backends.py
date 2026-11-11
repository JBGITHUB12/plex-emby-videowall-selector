#!/usr/bin/env python3
"""
Unified Local Media Backend Drivers for Plex, Emby, and Jellyfin (+ Optional Home Assistant Cast).

Capabilities:
  1. Catalog & Collection Sync:
     - Plex: Fetches Movie libraries + Plex Collections (/collections -> /children), placing
       multi-film franchises consecutively in viewing/release order followed by sort-title films.
     - Emby / Jellyfin: Fetches Movie items + BoxSet collections (/Items?IncludeItemTypes=BoxSet),
       placing multi-film BoxSets consecutively in release order followed by SortName films.
  2. Metadata & Token-Safe Artwork Proxying:
     - Streams poster and backdrop images through the VideoWall server so API tokens are never
       exposed in browser URLs.
  3. Zero-Search Local Network Player Discovery & 1-Tap Cast:
     - Plex Companion (/clients + /playQueues + /player/playback/playMedia, with server-proxied fallback).
     - Emby & Jellyfin Remote Control (/Sessions where SupportsRemoteControl=true -> /Sessions/{id}/Playing).
     - Optional Home Assistant media_player entities & Smart TV auto-wake/cast targets.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from config import AppConfig, ServerEntry, map_server_path

CLIENT_ID = "local-media-videowall-selector"
CLIENT_NAME = "Local Media VideoWall Selector"
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]")


def clean_sort_title(title: str) -> str:
    s = (title or "").strip().lower()
    for prefix in ("the ", "a ", "an "):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
            break
    return s


def sanitize_clip_key(raw_id: str) -> str:
    return SAFE_ID_RE.sub("_", str(raw_id).strip())


# =====================================================================
# PLEX MEDIA SERVER BACKEND
# =====================================================================
class PlexBackend:
    def __init__(self, entry: ServerEntry, key_prefix: str = "") -> None:
        self.entry = entry
        self.key_prefix = key_prefix
        self._machine_id_cache: str = entry.machine_id
        self._section_ids_cache: list[tuple[str, str]] | None = None

    def make_key(self, raw_id: str) -> str:
        clean = sanitize_clip_key(raw_id)
        return f"{self.key_prefix}__{clean}" if self.key_prefix else clean

    def _get_json(self, path: str, timeout: float = 15.0) -> dict[str, Any]:
        sep = "&" if "?" in path else "?"
        url = f"{self.entry.url}{path}{sep}X-Plex-Token={urllib.parse.quote(self.entry.token)}"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "X-Plex-Client-Identifier": CLIENT_ID,
                "X-Plex-Product": CLIENT_NAME,
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_machine_id(self) -> str:
        if self._machine_id_cache:
            return self._machine_id_cache
        for endpoint in ("/identity", "/"):
            try:
                data = self._get_json(endpoint, timeout=5.0)
                mid = str(data.get("MediaContainer", {}).get("machineIdentifier") or "").strip()
                if mid:
                    self._machine_id_cache = mid
                    return mid
            except Exception:
                continue
        return ""

    def get_movie_sections(self) -> list[tuple[str, str]]:
        if self._section_ids_cache is not None:
            return self._section_ids_cache
        if self.entry.section_id:
            parts = [s.strip() for s in self.entry.section_id.split(",") if s.strip()]
            self._section_ids_cache = [(sid, self.entry.library_name or "Movies") for sid in parts]
            return self._section_ids_cache

        sections: list[tuple[str, str]] = []
        data = self._get_json("/library/sections", timeout=10.0)
        for d in data.get("MediaContainer", {}).get("Directory", []) or []:
            if str(d.get("type") or "").lower() == "movie":
                sid = str(d.get("key") or "").strip()
                title = str(d.get("title") or "Movies").strip()
                if sid:
                    sections.append((sid, title))
        self._section_ids_cache = sections
        return sections

    def _fetch_section_collections(self, section_id: str) -> list[dict[str, Any]]:
        try:
            cols_meta = (
                self._get_json(f"/library/sections/{section_id}/collections", timeout=15.0)
                .get("MediaContainer", {})
                .get("Metadata", [])
            )
        except Exception as exc:
            print(f"[WARN] Could not fetch Plex collections for section {section_id}: {exc}", flush=True)
            return []

        def _load_one(col_obj: dict[str, Any]) -> dict[str, Any] | None:
            col_rk = str(col_obj.get("ratingKey", "")).strip()
            if not col_rk:
                return None
            col_title = (col_obj.get("title") or "").strip()
            col_sort = clean_sort_title(col_obj.get("titleSort") or col_title)
            try:
                kids = (
                    self._get_json(f"/library/collections/{col_rk}/children", timeout=15.0)
                    .get("MediaContainer", {})
                    .get("Metadata", [])
                )
            except Exception:
                return None
            kid_rks = [str(k.get("ratingKey", "")).strip() for k in kids if str(k.get("ratingKey", "")).strip()]
            if not kid_rks:
                return None
            return {
                "title": col_title,
                "sort_key": col_sort,
                "kid_ids": kid_rks,
            }

        results: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            for item in pool.map(_load_one, cols_meta):
                if item:
                    results.append(item)

        results.sort(key=lambda c: (0 if len(c["kid_ids"]) >= 2 else 1, c["sort_key"], -len(c["kid_ids"])))
        return results

    def list_movies_with_collections(self) -> list[dict[str, Any]]:
        by_id: dict[str, dict[str, Any]] = {}
        all_collections: list[dict[str, Any]] = []

        for section_id, section_title in self.get_movie_sections():
            start = 0
            page_size = 500
            total_size: int | None = None

            while total_size is None or start < total_size:
                data = self._get_json(
                    f"/library/sections/{section_id}/all"
                    f"?type=1&X-Plex-Container-Start={start}&X-Plex-Container-Size={page_size}",
                    timeout=20.0,
                )
                mc = data.get("MediaContainer", {})
                if total_size is None:
                    total_size = int(mc.get("totalSize", 0))
                batch = mc.get("Metadata", []) or []
                if not batch:
                    break

                for item in batch:
                    rk = str(item.get("ratingKey", "")).strip()
                    if not rk:
                        continue
                    title = item.get("title") or f"Movie {rk}"
                    title_sort = clean_sort_title(item.get("titleSort") or title)
                    year = item.get("year")
                    duration_ms = int(item.get("duration") or 0)
                    server_file = ""
                    part_key = ""
                    media_list = item.get("Media") or []
                    if media_list:
                        parts = media_list[0].get("Part") or []
                        if parts:
                            server_file = parts[0].get("file", "")
                            part_key = parts[0].get("key", "")
                            if not duration_ms:
                                duration_ms = int(parts[0].get("duration") or 0)

                    local_path = map_server_path(server_file, self.entry.path_mappings)
                    stream_url = (
                        f"{self.entry.url}{part_key}?X-Plex-Token={urllib.parse.quote(self.entry.token)}"
                        if part_key
                        else ""
                    )

                    by_id[rk] = {
                        "key": self.make_key(rk),
                        "raw_id": rk,
                        "provider": "plex",
                        "server_id": self.entry.id,
                        "library_name": section_title,
                        "title": title,
                        "title_sort": title_sort,
                        "year": year,
                        "duration_ms": duration_ms,
                        "local_path": local_path,
                        "stream_url": stream_url,
                        "collection": "",
                    }
                start += len(batch)

            all_collections.extend(self._fetch_section_collections(section_id))

        all_collections.sort(key=lambda c: (0 if len(c["kid_ids"]) >= 2 else 1, c["sort_key"], -len(c["kid_ids"])))

        ordered: list[dict[str, Any]] = []
        placed: set[str] = set()

        for col in all_collections:
            present = [rk for rk in col["kid_ids"] if rk in by_id and rk not in placed]
            if len(present) < 2:
                continue
            for rk in present:
                m = by_id[rk]
                m["collection"] = col["title"]
                ordered.append(m)
                placed.add(rk)

        remaining = [m for rk, m in by_id.items() if rk not in placed]
        remaining.sort(
            key=lambda m: (
                m["title_sort"],
                m["year"] or 9999,
                int(m["raw_id"]) if str(m["raw_id"]).isdigit() else 0,
            )
        )
        ordered.extend(remaining)
        return ordered

    def get_movie_details(self, raw_id: str, clip_key: str) -> dict[str, Any]:
        data = self._get_json(f"/library/metadata/{urllib.parse.quote(raw_id)}", timeout=8.0)
        items = data.get("MediaContainer", {}).get("Metadata", [])
        if not items:
            raise KeyError(f"Movie {raw_id} not found on Plex")
        m = items[0]

        duration_sec = int((m.get("duration") or 0) / 1000)
        view_offset_sec = int((m.get("viewOffset") or 0) / 1000)

        media0 = (m.get("Media") or [{}])[0]
        part0 = (media0.get("Part") or [{}])[0]

        container = (media0.get("container") or part0.get("container") or "mp4").lower()
        vcodec = (media0.get("videoCodec") or "h264").lower()
        acodec = (media0.get("audioCodec") or "aac").lower()
        res = str(media0.get("videoResolution") or "").upper()
        if res.isdigit():
            res = f"{res}p"

        part_key = part0.get("key") or ""
        server_file = part0.get("file") or ""
        local_path = map_server_path(server_file, self.entry.path_mappings)

        direct_stream = (
            f"{self.entry.url}{part_key}?X-Plex-Token={urllib.parse.quote(self.entry.token)}"
            if part_key
            else ""
        )
        hls_stream = (
            f"{self.entry.url}/video/:/transcode/universal/start.m3u8"
            f"?path={urllib.parse.quote('/library/metadata/' + str(raw_id))}"
            f"&mediaIndex=0&partIndex=0&protocol=hls&directPlay=0&directStream=1"
            f"&videoQuality=100&maxVideoBitrate=25000"
            f"&X-Plex-Platform=Chrome&X-Plex-Client-Identifier={CLIENT_ID}-{raw_id}"
            f"&X-Plex-Token={urllib.parse.quote(self.entry.token)}"
        )

        browser_native_audio = acodec in ("aac", "mp3", "opus", "vorbis", "flac", "")
        browser_native_container = container in ("mp4", "m4v", "mov", "webm")

        if browser_native_container and browser_native_audio and direct_stream:
            play_here_url = direct_stream
            play_here_mode = "direct"
        else:
            play_here_url = f"/api/remux/{urllib.parse.quote(clip_key)}"
            play_here_mode = "remux"

        genres = [g.get("tag") for g in (m.get("Genre") or []) if g.get("tag")][:3]
        lib_title = str(m.get("librarySectionTitle") or self.entry.library_name or "Movies")

        return {
            "key": clip_key,
            "raw_id": str(m.get("ratingKey", raw_id)),
            "provider": "plex",
            "server_name": self.entry.name or "Plex",
            "library_name": lib_title,
            "title": m.get("title") or f"Movie {raw_id}",
            "year": m.get("year"),
            "tagline": m.get("tagline") or "",
            "summary": m.get("summary") or "",
            "contentRating": (m.get("contentRating") or "").replace("gb/", ""),
            "rating": m.get("audienceRating") or m.get("rating"),
            "duration_sec": duration_sec,
            "view_offset_sec": view_offset_sec,
            "resolution": res or "HD",
            "video_codec": vcodec,
            "audio_codec": acodec,
            "container": container,
            "genres": genres,
            "poster_url": f"/api/poster/{urllib.parse.quote(clip_key)}",
            "art_url": f"/api/art/{urllib.parse.quote(clip_key)}",
            "play_here_url": play_here_url,
            "play_here_mode": play_here_mode,
            "direct_stream": direct_stream,
            "hls_stream": hls_stream,
            "local_path": local_path,
            "external_thumb_url": (
                f"{self.entry.url}{m.get('thumb')}?X-Plex-Token={urllib.parse.quote(self.entry.token)}"
                if m.get("thumb")
                else ""
            ),
        }

    def get_image_bytes(self, raw_id: str, kind: str) -> tuple[bytes, str]:
        is_poster = kind == "poster"
        w, h = (340, 510) if is_poster else (768, 432)
        sub = "thumb" if is_poster else "art"
        img_url = (
            f"{self.entry.url}/photo/:/transcode?width={w}&height={h}&minSize=1&upscale=1"
            f"&url={urllib.parse.quote(f'/library/metadata/{raw_id}/{sub}')}"
            f"&X-Plex-Token={urllib.parse.quote(self.entry.token)}"
        )
        req = urllib.request.Request(img_url)
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            return resp.read(), (resp.headers.get("Content-Type") or "image/jpeg")

    def discover_players(self) -> list[dict[str, Any]]:
        players: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        try:
            cl_data = self._get_json("/clients", timeout=3.5)
            for srv in cl_data.get("MediaContainer", {}).get("Server", []) or []:
                mid = str(srv.get("machineIdentifier") or "").strip()
                addr = str(srv.get("address") or srv.get("host") or "").strip()
                port = srv.get("port") or 32500
                name = str(srv.get("name") or srv.get("product") or "Plex Player").strip()
                product = str(srv.get("product") or "Plex Client").strip()
                if mid and addr and mid not in seen_ids:
                    seen_ids.add(mid)
                    players.append(
                        {
                            "id": f"plex_companion:{self.entry.id}:{mid}:{addr}:{port}",
                            "name": name,
                            "subtitle": f"{product} • {self.entry.name or 'Plex'}",
                            "icon": "plex",
                            "state": "ready",
                            "badge": "PLEX READY",
                        }
                    )
        except Exception:
            pass
        return players

    def cast_to_companion(self, raw_id: str, target_spec: str, offset_sec: int = 0) -> dict[str, Any]:
        # target_spec format: "{mid}:{addr}:{port}"
        mid, addr, port_s = target_spec.split(":", 2)
        machine_id = self.get_machine_id()
        parsed_srv = urllib.parse.urlparse(self.entry.url)
        srv_host = parsed_srv.hostname or "127.0.0.1"
        srv_port = parsed_srv.port or (443 if parsed_srv.scheme == "https" else 32400)
        srv_proto = parsed_srv.scheme or "http"

        offset_ms = max(0, int(offset_sec * 1000))
        pq_id = ""
        if machine_id:
            uri = f"server://{machine_id}/com.plexapp.plugins.library/library/metadata/{raw_id}"
            pq_req = urllib.request.Request(
                f"{self.entry.url}/playQueues?type=video&uri={urllib.parse.quote(uri, safe='')}"
                f"&shuffle=0&repeat=0&own=1&X-Plex-Token={urllib.parse.quote(self.entry.token)}",
                headers={"Accept": "application/json", "X-Plex-Client-Identifier": CLIENT_ID},
                method="POST",
            )
            try:
                with urllib.request.urlopen(pq_req, timeout=6.0) as resp:
                    pq_data = json.loads(resp.read().decode("utf-8"))
                    pq_id = str(pq_data.get("MediaContainer", {}).get("playQueueID") or "")
            except Exception:
                pass

        params: dict[str, str] = {
            "key": f"/library/metadata/{raw_id}",
            "offset": str(offset_ms),
            "machineIdentifier": machine_id,
            "address": srv_host,
            "port": str(srv_port),
            "protocol": srv_proto,
            "commandID": str(int(time.time()) % 100000),
            "token": self.entry.token,
        }
        if pq_id:
            params["containerKey"] = f"/playQueues/{pq_id}?window=100&own=1"

        headers = {
            "X-Plex-Client-Identifier": CLIENT_ID,
            "X-Plex-Device-Name": CLIENT_NAME,
            "X-Plex-Target-Client-Identifier": mid,
            "X-Plex-Token": self.entry.token,
        }
        qs = urllib.parse.urlencode(params)

        # Try direct LAN Plex Companion first, then server-proxied fallback across subnets
        direct_url = f"http://{addr}:{port_s}/player/playback/playMedia?{qs}"
        proxy_url = f"{self.entry.url}/system/players/{addr}/playback/playMedia?{qs}"

        last_err = ""
        for url in (direct_url, proxy_url):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=7.0) as resp:
                    resp.read()
                return {"ok": True, "message": "Started playback via Plex Companion"}
            except Exception as exc:
                last_err = str(exc)

        return {"ok": False, "message": f"Plex Companion error: {last_err}"}


# =====================================================================
# EMBY & JELLYFIN MEDIA SERVER BACKEND
# =====================================================================
class EmbyJellyfinBackend:
    def __init__(self, entry: ServerEntry, key_prefix: str = "") -> None:
        self.entry = entry
        self.key_prefix = key_prefix
        self.provider = "jellyfin" if entry.provider == "jellyfin" else "emby"
        self.label = self.entry.name or ("Jellyfin" if self.provider == "jellyfin" else "Emby")
        self._user_id_cache: str = entry.user_id

    def make_key(self, raw_id: str) -> str:
        clean = sanitize_clip_key(raw_id)
        return f"{self.key_prefix}__{clean}" if self.key_prefix else clean

    def _headers(self, json_body: bool = False) -> dict[str, str]:
        auth_header = (
            f'MediaBrowser Client="{CLIENT_NAME}", Device="VideoWall", '
            f'DeviceId="{CLIENT_ID}", Version="1.0.0"'
        )
        if self.entry.token:
            auth_header += f', Token="{self.entry.token}"'
        hdrs = {
            "Accept": "application/json",
            "X-Emby-Token": self.entry.token,
            "X-MediaBrowser-Token": self.entry.token,
            "Authorization": auth_header,
        }
        if json_body:
            hdrs["Content-Type"] = "application/json"
        return hdrs

    def _get_json(self, path: str, timeout: float = 15.0) -> Any:
        url = f"{self.entry.url}{path}"
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}

    def _post(self, path: str, payload: dict[str, Any] | None = None, timeout: float = 8.0) -> None:
        url = f"{self.entry.url}{path}"
        body = json.dumps(payload or {}).encode("utf-8") if payload is not None else b""
        req = urllib.request.Request(url, data=body, headers=self._headers(json_body=True), method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()

    def get_user_id(self) -> str:
        if self._user_id_cache:
            return self._user_id_cache
        try:
            users = self._get_json("/Users", timeout=8.0)
            if isinstance(users, list) and users:
                for u in users:
                    if (u.get("Policy") or {}).get("IsAdministrator"):
                        self._user_id_cache = str(u.get("Id") or "")
                        return self._user_id_cache
                self._user_id_cache = str(users[0].get("Id") or "")
        except Exception:
            pass
        return self._user_id_cache

    def _items_prefix(self) -> str:
        uid = self.get_user_id()
        return f"/Users/{urllib.parse.quote(uid)}/Items" if uid else "/Items"

    def _fetch_boxsets(self) -> list[dict[str, Any]]:
        prefix = self._items_prefix()
        try:
            box_data = self._get_json(
                f"{prefix}?IncludeItemTypes=BoxSet&Recursive=true&Fields=SortName",
                timeout=15.0,
            )
            box_items = box_data.get("Items") or []
        except Exception as exc:
            print(f"[WARN] Could not fetch {self.label} BoxSets: {exc}", flush=True)
            return []

        def _load_one(box: dict[str, Any]) -> dict[str, Any] | None:
            bid = str(box.get("Id") or "").strip()
            if not bid:
                return None
            btitle = str(box.get("Name") or "").strip()
            bsort = clean_sort_title(box.get("SortName") or btitle)
            try:
                kids_resp = self._get_json(
                    f"{prefix}?ParentId={urllib.parse.quote(bid)}"
                    f"&IncludeItemTypes=Movie&SortBy=PremiereDate,SortName&SortOrder=Ascending",
                    timeout=12.0,
                )
                kids = kids_resp.get("Items") or []
            except Exception:
                return None
            kid_ids = [str(k.get("Id") or "").strip() for k in kids if str(k.get("Id") or "").strip()]
            if not kid_ids:
                return None
            return {"title": btitle, "sort_key": bsort, "kid_ids": kid_ids}

        results: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            for item in pool.map(_load_one, box_items):
                if item:
                    results.append(item)

        results.sort(key=lambda c: (0 if len(c["kid_ids"]) >= 2 else 1, c["sort_key"], -len(c["kid_ids"])))
        return results

    def list_movies_with_collections(self) -> list[dict[str, Any]]:
        prefix = self._items_prefix()
        by_id: dict[str, dict[str, Any]] = {}

        parent_filters = [s.strip() for s in self.entry.section_id.split(",") if s.strip()] or [""]

        for parent_id in parent_filters:
            start = 0
            limit = 500
            total: int | None = None

            while total is None or start < total:
                parent_q = f"&ParentId={urllib.parse.quote(parent_id)}" if parent_id else ""
                data = self._get_json(
                    f"{prefix}?IncludeItemTypes=Movie&Recursive=true"
                    f"&Fields=Path,MediaSources,SortName,ProductionYear"
                    f"&StartIndex={start}&Limit={limit}{parent_q}",
                    timeout=25.0,
                )
                if total is None:
                    total = int(data.get("TotalRecordCount") or 0)
                items = data.get("Items") or []
                if not items:
                    break

                for item in items:
                    iid = str(item.get("Id") or "").strip()
                    if not iid:
                        continue
                    title = str(item.get("Name") or f"Movie {iid}")
                    title_sort = clean_sort_title(item.get("SortName") or title)
                    year = item.get("ProductionYear")
                    ticks = int(item.get("RunTimeTicks") or 0)
                    duration_ms = ticks // 10_000

                    ms_list = item.get("MediaSources") or []
                    ms0 = ms_list[0] if ms_list else {}
                    ms_id = str(ms0.get("Id") or iid)
                    container = str(ms0.get("Container") or "mp4").split(",")[0].lower()
                    server_file = str(ms0.get("Path") or item.get("Path") or "")

                    local_path = map_server_path(server_file, self.entry.path_mappings)
                    stream_url = (
                        f"{self.entry.url}/Videos/{urllib.parse.quote(iid)}/stream.{container}"
                        f"?Static=true&MediaSourceId={urllib.parse.quote(ms_id)}"
                        f"&api_key={urllib.parse.quote(self.entry.token)}"
                    )

                    by_id[iid] = {
                        "key": self.make_key(iid),
                        "raw_id": iid,
                        "provider": self.provider,
                        "server_id": self.entry.id,
                        "library_name": self.entry.library_name or "Movies",
                        "title": title,
                        "title_sort": title_sort,
                        "year": year,
                        "duration_ms": duration_ms,
                        "local_path": local_path,
                        "stream_url": stream_url,
                        "collection": "",
                    }
                start += len(items)

        boxsets = self._fetch_boxsets()
        ordered: list[dict[str, Any]] = []
        placed: set[str] = set()

        for col in boxsets:
            present = [iid for iid in col["kid_ids"] if iid in by_id and iid not in placed]
            if len(present) < 2:
                continue
            for iid in present:
                m = by_id[iid]
                m["collection"] = col["title"]
                ordered.append(m)
                placed.add(iid)

        remaining = [m for iid, m in by_id.items() if iid not in placed]
        remaining.sort(key=lambda m: (m["title_sort"], m["year"] or 9999, m["raw_id"]))
        ordered.extend(remaining)
        return ordered

    def get_movie_details(self, raw_id: str, clip_key: str) -> dict[str, Any]:
        uid = self.get_user_id()
        if uid:
            m = self._get_json(f"/Users/{urllib.parse.quote(uid)}/Items/{urllib.parse.quote(raw_id)}", timeout=8.0)
        else:
            m = self._get_json(
                f"/Items?Ids={urllib.parse.quote(raw_id)}"
                f"&Fields=Overview,Genres,MediaSources,Path,OfficialRating,CommunityRating,Taglines",
                timeout=8.0,
            )
            items = m.get("Items") or []
            if not items:
                raise KeyError(f"Movie {raw_id} not found on {self.label}")
            m = items[0]

        duration_sec = int((m.get("RunTimeTicks") or 0) / 10_000_000)
        user_data = m.get("UserData") or {}
        view_offset_sec = int((user_data.get("PlaybackPositionTicks") or 0) / 10_000_000)

        ms_list = m.get("MediaSources") or [{}]
        ms0 = ms_list[0]
        ms_id = str(ms0.get("Id") or raw_id)
        container = str(ms0.get("Container") or "mp4").split(",")[0].lower()
        server_file = str(ms0.get("Path") or m.get("Path") or "")
        local_path = map_server_path(server_file, self.entry.path_mappings)

        vcodec = "h264"
        acodec = "aac"
        res = "HD"
        for st in ms0.get("MediaStreams") or []:
            st_type = str(st.get("Type") or "")
            if st_type == "Video" and vcodec == "h264":
                vcodec = str(st.get("Codec") or "h264").lower()
                height = int(st.get("Height") or 0)
                width = int(st.get("Width") or 0)
                if width >= 3800 or height >= 2000:
                    res = "4K"
                elif width >= 1900 or height >= 1000:
                    res = "1080p"
                elif height >= 700:
                    res = "720p"
            elif st_type == "Audio" and acodec == "aac":
                acodec = str(st.get("Codec") or "aac").lower()

        direct_stream = (
            f"{self.entry.url}/Videos/{urllib.parse.quote(raw_id)}/stream.{container}"
            f"?Static=true&MediaSourceId={urllib.parse.quote(ms_id)}"
            f"&api_key={urllib.parse.quote(self.entry.token)}"
        )
        hls_stream = (
            f"{self.entry.url}/Videos/{urllib.parse.quote(raw_id)}/master.m3u8"
            f"?MediaSourceId={urllib.parse.quote(ms_id)}"
            f"&VideoCodec=h264&AudioCodec=aac&MaxStreamingBitrate=25000000"
            f"&DeviceId={CLIENT_ID}-{raw_id}&api_key={urllib.parse.quote(self.entry.token)}"
        )

        browser_native_audio = acodec in ("aac", "mp3", "opus", "vorbis", "flac", "")
        browser_native_container = container in ("mp4", "m4v", "mov", "webm")

        if browser_native_container and browser_native_audio:
            play_here_url = direct_stream
            play_here_mode = "direct"
        else:
            play_here_url = f"/api/remux/{urllib.parse.quote(clip_key)}"
            play_here_mode = "remux"

        taglines = m.get("Taglines") or []
        genres = list(m.get("Genres") or [])[:3]

        return {
            "key": clip_key,
            "raw_id": str(m.get("Id") or raw_id),
            "provider": self.provider,
            "server_name": self.label,
            "library_name": self.entry.library_name or "Movies",
            "title": m.get("Name") or f"Movie {raw_id}",
            "year": m.get("ProductionYear"),
            "tagline": taglines[0] if taglines else "",
            "summary": m.get("Overview") or "",
            "contentRating": m.get("OfficialRating") or "",
            "rating": m.get("CommunityRating"),
            "duration_sec": duration_sec,
            "view_offset_sec": view_offset_sec,
            "resolution": res,
            "video_codec": vcodec,
            "audio_codec": acodec,
            "container": container,
            "genres": genres,
            "poster_url": f"/api/poster/{urllib.parse.quote(clip_key)}",
            "art_url": f"/api/art/{urllib.parse.quote(clip_key)}",
            "play_here_url": play_here_url,
            "play_here_mode": play_here_mode,
            "direct_stream": direct_stream,
            "hls_stream": hls_stream,
            "local_path": local_path,
            "external_thumb_url": (
                f"{self.entry.url}/Items/{urllib.parse.quote(raw_id)}/Images/Primary"
                f"?maxWidth=400&api_key={urllib.parse.quote(self.entry.token)}"
            ),
        }

    def get_image_bytes(self, raw_id: str, kind: str) -> tuple[bytes, str]:
        q_id = urllib.parse.quote(raw_id)
        candidates = (
            [f"/Items/{q_id}/Images/Primary?maxWidth=360&quality=85"]
            if kind == "poster"
            else [
                f"/Items/{q_id}/Images/Backdrop/0?maxWidth=768&quality=80",
                f"/Items/{q_id}/Images/Primary?maxWidth=600&quality=80",
            ]
        )
        last_exc: Exception | None = None
        for rel in candidates:
            try:
                req = urllib.request.Request(f"{self.entry.url}{rel}", headers=self._headers())
                with urllib.request.urlopen(req, timeout=8.0) as resp:
                    return resp.read(), (resp.headers.get("Content-Type") or "image/jpeg")
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(f"Image fetch failed for {raw_id}: {last_exc}")

    def discover_players(self) -> list[dict[str, Any]]:
        players: list[dict[str, Any]] = []
        seen_sessions: set[str] = set()
        badge = "JELLYFIN READY" if self.provider == "jellyfin" else "EMBY READY"
        try:
            sessions = self._get_json("/Sessions", timeout=4.0)
            for s in sessions if isinstance(sessions, list) else []:
                sid = str(s.get("Id") or "").strip()
                client_app = str(s.get("Client") or "").strip()
                dev_name = str(s.get("DeviceName") or client_app or "Media Player").strip()
                if not sid or sid in seen_sessions or client_app == CLIENT_NAME:
                    continue
                if not s.get("SupportsRemoteControl", False):
                    continue
                playable = s.get("PlayableMediaTypes") or []
                if playable and "Video" not in playable:
                    continue
                seen_sessions.add(sid)
                user_name = str(s.get("UserName") or "").strip()
                sub_parts = [p for p in (client_app, user_name, self.label) if p]
                players.append(
                    {
                        "id": f"emby_session:{self.entry.id}:{sid}",
                        "name": dev_name,
                        "subtitle": " • ".join(sub_parts),
                        "icon": self.provider,
                        "state": "ready",
                        "badge": badge,
                    }
                )
        except Exception:
            pass
        return players

    def cast_to_session(self, raw_id: str, session_id: str, offset_sec: int = 0) -> dict[str, Any]:
        ticks = max(0, int(offset_sec * 10_000_000))
        qs = urllib.parse.urlencode(
            {
                "ItemIds": raw_id,
                "PlayCommand": "PlayNow",
                "StartPositionTicks": str(ticks),
            }
        )
        self._post(f"/Sessions/{urllib.parse.quote(session_id)}/Playing?{qs}")
        return {"ok": True, "message": f"Started playback via {self.label} Remote Control"}


# =====================================================================
# MULTI-SERVER ROUTER + HOME ASSISTANT / CAST DISPATCHER
# =====================================================================
class MediaRouter:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        use_prefix = len(cfg.servers) > 1
        self.backends: dict[str, PlexBackend | EmbyJellyfinBackend] = {}
        self.ordered_backends: list[PlexBackend | EmbyJellyfinBackend] = []

        for srv in cfg.servers:
            prefix = sanitize_clip_key(srv.id) if use_prefix else ""
            if srv.provider == "plex":
                b: PlexBackend | EmbyJellyfinBackend = PlexBackend(srv, key_prefix=prefix)
            else:
                b = EmbyJellyfinBackend(srv, key_prefix=prefix)
            self.backends[srv.id] = b
            self.ordered_backends.append(b)

    def resolve_clip_key(self, clip_key: str) -> tuple[PlexBackend | EmbyJellyfinBackend, str]:
        if not self.ordered_backends:
            raise RuntimeError("No media servers configured. Set MEDIA_URL and MEDIA_TOKEN in .env or config.json.")
        if "__" in clip_key and len(self.ordered_backends) > 1:
            prefix, raw_id = clip_key.split("__", 1)
            for b in self.ordered_backends:
                if sanitize_clip_key(b.entry.id) == prefix:
                    return b, raw_id
        return self.ordered_backends[0], clip_key

    def list_all_movies(self) -> list[dict[str, Any]]:
        combined: list[dict[str, Any]] = []
        for b in self.ordered_backends:
            combined.extend(b.list_movies_with_collections())
        return combined

    def get_movie_details(self, clip_key: str) -> dict[str, Any]:
        backend, raw_id = self.resolve_clip_key(clip_key)
        return backend.get_movie_details(raw_id, clip_key)

    def get_image_bytes(self, clip_key: str, kind: str) -> tuple[bytes, str]:
        backend, raw_id = self.resolve_clip_key(clip_key)
        return backend.get_image_bytes(raw_id, kind)

    def _ha_get_states(self, timeout: float = 4.0) -> dict[str, dict[str, Any]]:
        if not self.cfg.ha_url or not self.cfg.ha_token:
            return {}
        req = urllib.request.Request(
            f"{self.cfg.ha_url}/api/states",
            headers={"Authorization": f"Bearer {self.cfg.ha_token}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            arr = json.loads(resp.read().decode("utf-8"))
        return {item["entity_id"]: item for item in arr if isinstance(item, dict) and "entity_id" in item}

    def _ha_call_service(self, domain: str, service: str, payload: dict[str, Any], timeout: float = 8.0) -> None:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.cfg.ha_url}/api/services/{domain}/{service}",
            data=data,
            headers={
                "Authorization": f"Bearer {self.cfg.ha_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()

    def discover_players(self) -> list[dict[str, Any]]:
        devices: list[dict[str, Any]] = []
        seen_ha_entities: set[str] = set()

        # 1. Native LAN players from every configured Plex / Emby / Jellyfin server
        for b in self.ordered_backends:
            devices.extend(b.discover_players())

        # 2. Optional Home Assistant Smart TVs, Google Cast & Media Player entities
        if not self.cfg.ha_url or not self.cfg.ha_token:
            return devices

        try:
            ha_states = self._ha_get_states(timeout=4.0)
        except Exception:
            ha_states = {}

        for target in self.cfg.cast_targets:
            tid = str(target.get("id") or "").strip()
            tname = str(target.get("name") or tid).strip()
            if not tid or not tname:
                continue

            player_ents = target.get("player_entities") or target.get("plex_entities") or []
            active_app_ent = ""
            for pe in player_ents:
                seen_ha_entities.add(pe)
                st_obj = ha_states.get(pe)
                if st_obj and st_obj.get("state") not in ("unavailable", "unknown", None):
                    active_app_ent = pe
                    break

            pwr_ent = str(target.get("power_entity") or target.get("cast_entity") or "")
            cast_ent = str(target.get("cast_entity") or "")
            pwr_state = (ha_states.get(pwr_ent) or {}).get("state", "unknown")
            cast_state = (ha_states.get(cast_ent) or {}).get("state", "unknown")

            if active_app_ent:
                badge = "APP READY"
                state_tag = "ready"
            elif pwr_state in ("on", "playing", "paused", "idle", "buffering") or cast_state in (
                "on",
                "playing",
                "paused",
                "idle",
            ):
                badge = "ONLINE"
                state_tag = "online"
            elif not cast_ent and not active_app_ent:
                badge = "OPEN APP FIRST"
                state_tag = "offline"
            else:
                badge = "STANDBY • AUTO-WAKE"
                state_tag = "standby"

            devices.append(
                {
                    "id": f"cast_target:{tid}",
                    "name": tname,
                    "subtitle": str(target.get("subtitle") or "Smart TV / Cast Display"),
                    "icon": str(target.get("icon") or "tv"),
                    "state": state_tag,
                    "badge": badge,
                }
            )

        # Auto-discover active Plex / Emby / Jellyfin players in Home Assistant
        for eid, st_obj in ha_states.items():
            if eid in seen_ha_entities or not eid.startswith("media_player."):
                continue
            if "plexamp" in eid:
                continue
            is_plex = eid.startswith("media_player.plex_")
            is_emby_jf = eid.startswith(("media_player.emby_", "media_player.jellyfin_"))
            if not (is_plex or is_emby_jf):
                continue
            if st_obj.get("state") not in ("unavailable", "unknown", None):
                fname = (st_obj.get("attributes") or {}).get("friendly_name") or eid
                devices.append(
                    {
                        "id": f"ha_entity:{eid}",
                        "name": str(fname),
                        "subtitle": "Home Assistant Media Player",
                        "icon": "plex" if is_plex else "emby",
                        "state": "ready",
                        "badge": "PLAYER READY",
                    }
                )

        return devices

    def cast_movie(self, clip_key: str, target_id: str, offset_sec: int = 0) -> dict[str, Any]:
        backend, raw_id = self.resolve_clip_key(clip_key)
        movie = backend.get_movie_details(raw_id, clip_key)
        title = movie["title"]

        # 1. Direct Plex Companion
        if target_id.startswith("plex_companion:"):
            _, srv_id, rest = target_id.split(":", 2)
            target_backend = self.backends.get(srv_id, backend)
            if isinstance(target_backend, PlexBackend):
                res = target_backend.cast_to_companion(raw_id, rest, offset_sec)
                if res.get("ok"):
                    res["message"] = f"Playing '{title}' via Plex Companion"
                return res
            return {"ok": False, "message": "Selected Plex player does not match movie backend"}

        # 2. Direct Emby / Jellyfin Remote Control Session
        if target_id.startswith("emby_session:"):
            _, srv_id, session_id = target_id.split(":", 2)
            target_backend = self.backends.get(srv_id, backend)
            if isinstance(target_backend, EmbyJellyfinBackend):
                res = target_backend.cast_to_session(raw_id, session_id, offset_sec)
                if res.get("ok"):
                    res["message"] = f"Playing '{title}' on {target_backend.label} Player"
                return res
            return {"ok": False, "message": "Selected Emby/Jellyfin player does not match movie backend"}

        # 3. Direct Home Assistant media_player entity
        if target_id.startswith("ha_entity:"):
            eid = target_id.split(":", 1)[1]
            return self._dispatch_ha_entity(eid, movie, offset_sec)

        # 4. Configured Smart TV / Cast Target in Home Assistant
        if target_id.startswith("cast_target:"):
            tid = target_id.split(":", 1)[1]
            target_cfg = next((t for t in self.cfg.cast_targets if str(t.get("id")) == tid), None)
            if not target_cfg:
                return {"ok": False, "message": f"Unknown cast target: {tid}"}
            return self._dispatch_cast_target(target_cfg, movie, offset_sec)

        return {"ok": False, "message": f"Unsupported target device: {target_id}"}

    def _dispatch_ha_entity(self, entity_id: str, movie: dict[str, Any], offset_sec: int) -> dict[str, Any]:
        title = movie["title"]
        if entity_id.startswith("media_player.plex_") and movie["provider"] == "plex":
            plex_lookup: dict[str, Any] = {
                "library_name": movie.get("library_name") or "Movies",
                "title": title,
            }
            if movie.get("year"):
                plex_lookup["year"] = int(movie["year"])
            self._ha_call_service(
                "media_player",
                "play_media",
                {
                    "entity_id": entity_id,
                    "media_content_type": "movie",
                    "media_content_id": "plex://" + json.dumps(plex_lookup),
                },
            )
            return {"ok": True, "message": f"Playing '{title}' via Plex on Home Assistant"}

        return self._stream_url_to_ha_cast(entity_id, entity_id, movie, offset_sec)

    def _dispatch_cast_target(self, target_cfg: dict[str, Any], movie: dict[str, Any], offset_sec: int) -> dict[str, Any]:
        tname = str(target_cfg.get("name") or "TV")
        ha_states: dict[str, dict[str, Any]] = {}
        try:
            ha_states = self._ha_get_states(timeout=4.0)
        except Exception:
            pass

        player_ents = target_cfg.get("player_entities") or target_cfg.get("plex_entities") or []
        for pe in player_ents:
            st_obj = ha_states.get(pe)
            if st_obj and st_obj.get("state") not in ("unavailable", "unknown", None):
                res = self._dispatch_ha_entity(pe, movie, offset_sec)
                if res.get("ok"):
                    res["message"] = f"Playing '{movie['title']}' on {tname}"
                return res

        cast_ent = str(target_cfg.get("cast_entity") or "")
        pwr_ent = str(target_cfg.get("power_entity") or "")
        if not cast_ent:
            return {"ok": False, "message": f"Open your media app on {tname} first, then tap again."}

        # Auto-wake TV if asleep
        for wake_ent in {e for e in (pwr_ent, cast_ent) if e}:
            st_val = (ha_states.get(wake_ent) or {}).get("state")
            if st_val in ("off", "standby", "unavailable", None):
                try:
                    self._ha_call_service("media_player", "turn_on", {"entity_id": wake_ent}, timeout=6.0)
                except Exception:
                    pass

        time.sleep(0.5)
        res = self._stream_url_to_ha_cast(cast_ent, tname, movie, offset_sec)
        return res

    def _stream_url_to_ha_cast(
        self, cast_ent: str, friendly_name: str, movie: dict[str, Any], offset_sec: int
    ) -> dict[str, Any]:
        title = movie["title"]
        stream_url = movie.get("direct_stream") or ""
        if not stream_url or movie.get("container") not in ("mp4", "m4v", "mov"):
            stream_url = movie.get("hls_stream") or stream_url
            content_type = "application/x-mpegURL"
        else:
            content_type = "video/mp4"

        play_payload: dict[str, Any] = {
            "entity_id": cast_ent,
            "media_content_id": stream_url,
            "media_content_type": content_type,
            "extra": {
                "title": f"{title} ({movie['year']})" if movie.get("year") else title,
                "thumb": movie.get("external_thumb_url") or "",
            },
        }
        if offset_sec > 5:
            play_payload["extra"]["current_time"] = int(offset_sec)

        self._ha_call_service("media_player", "play_media", play_payload, timeout=10.0)
        return {"ok": True, "message": f"Casting '{title}' to {friendly_name}"}
