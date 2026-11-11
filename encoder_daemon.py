#!/usr/bin/env python3
"""
Local Media Plex, Emby & Jellyfin VideoWall Selector — Clip Sync & Multi-Worker Encoder
Syncs all movies and Collections / BoxSets from your configured Plex, Emby, or Jellyfin
server(s) and pre-encodes lightweight 40-second 120p (216x120) H.264 + normalized stereo AAC
mid-movie scene clips so 80+ tiles can play simultaneously with zero idle server transcode load.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from config import AppConfig, find_ffmpeg, load_config
from media_backends import MediaRouter


def atomic_write_json(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def encode_one_clip(
    ffmpeg_bin: str,
    movie: dict[str, Any],
    clips_dir: Path,
    worker_tag: str,
    cfg: AppConfig,
) -> tuple[str, bool, str]:
    key = str(movie["key"])
    final_mp4 = clips_dir / f"{key}.mp4"
    if final_mp4.exists() and final_mp4.stat().st_size > 16384:
        return key, True, "already_exists"

    # Prefer direct local/NAS file path if mapped & accessible; otherwise stream via HTTP from Plex/Emby/Jellyfin
    src = str(movie.get("local_path") or "")
    if not src or not os.path.exists(src):
        src = str(movie.get("stream_url") or "")
        if not src:
            return key, False, "missing_source"

    clip_dur = cfg.clip_duration_sec
    duration_sec = max(0.0, float(movie.get("duration_ms") or 0) / 1000.0)
    if duration_sec > (clip_dur + 120):
        seek_sec = int(duration_sec * cfg.clip_seek_ratio)
    elif duration_sec > (clip_dur + 10):
        seek_sec = int((duration_sec - clip_dur) * 0.5)
    else:
        seek_sec = 0

    tmp_mp4 = clips_dir / f"{key}.{worker_tag}.tmp.mp4"
    vf = (
        f"scale={cfg.tile_width}:{cfg.tile_height}:force_original_aspect_ratio=increase,"
        f"crop={cfg.tile_width}:{cfg.tile_height},setsar=1,fps=24"
    )

    cmd = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(seek_sec),
        "-i",
        src,
        "-t",
        str(clip_dur),
        "-threads",
        "2",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "25",
        "-profile:v",
        "main",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "48",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-af",
        "dynaudnorm=f=200:g=15",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(tmp_mp4),
    ]

    run_kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": 120,
        "stdin": subprocess.DEVNULL,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }

    try:
        res = subprocess.run(cmd, **run_kwargs)
        if res.returncode != 0 or not tmp_mp4.exists() or tmp_mp4.stat().st_size < 16384:
            # Fallback retry at 15s offset if container metadata had an inaccurate duration
            tmp_mp4.unlink(missing_ok=True)
            cmd[cmd.index("-ss") + 1] = "15"
            res = subprocess.run(cmd, **run_kwargs)
            if res.returncode != 0 or not tmp_mp4.exists() or tmp_mp4.stat().st_size < 8192:
                err = (res.stderr or "encode_failed").strip()[:200]
                tmp_mp4.unlink(missing_ok=True)
                return key, False, err

        os.replace(tmp_mp4, final_mp4)
        return key, True, "encoded"
    except Exception as exc:
        tmp_mp4.unlink(missing_ok=True)
        return key, False, str(exc)[:200]


def run_sync_pass(
    cfg: AppConfig,
    router: MediaRouter,
    wall_dir: Path,
    ffmpeg_bin: str,
    workers: int,
    shard_index: int,
    shard_total: int,
    limit: int = 0,
    catalog_only: bool = False,
) -> None:
    clips_dir = wall_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = wall_dir / "catalog.json"
    status_path = wall_dir / "encoder_status.json"

    if not router.ordered_backends:
        print(
            "[Encoder] No media servers configured yet. Set MEDIA_PROVIDER, MEDIA_URL, and MEDIA_TOKEN in .env.",
            flush=True,
        )
        return

    server_labels = ", ".join(f"{b.entry.provider.upper()} ({b.entry.url})" for b in router.ordered_backends)
    print(f"[Encoder] Syncing movies & collections from {server_labels}...", flush=True)

    movies = router.list_all_movies()
    total_movies = len(movies)
    col_movies = sum(1 for m in movies if m.get("collection"))
    col_names = len({m["collection"] for m in movies if m.get("collection")})
    print(
        f"[Encoder] Discovered {total_movies} movies "
        f"({col_movies} films grouped across {col_names} Collections/BoxSets first).",
        flush=True,
    )

    if shard_index == 0:
        compact_catalog = []
        for m in movies:
            entry: dict[str, Any] = {
                "k": m["key"],
                "t": m["title"],
                "y": m["year"],
            }
            if m.get("collection"):
                entry["c"] = m["collection"]
            if len(router.ordered_backends) > 1:
                entry["p"] = m["provider"]
            compact_catalog.append(entry)

        atomic_write_json(
            catalog_path,
            {
                "updated_at": time.time(),
                "total_movies": total_movies,
                "collections_count": col_names,
                "collection_movies_count": col_movies,
                "movies": compact_catalog,
            },
        )

    if catalog_only:
        return

    if limit > 0:
        movies = movies[:limit]

    existing = {
        p.stem
        for p in clips_dir.glob("*.mp4")
        if not p.name.endswith(".tmp.mp4") and p.stat().st_size > 8192
    }

    todo = [
        m
        for idx, m in enumerate(movies)
        if (idx % shard_total == shard_index) and (m["key"] not in existing)
    ]

    print(
        f"[Encoder] Shard {shard_index}/{shard_total}: {len(existing)} clips ready, "
        f"{len(todo)} queued across {workers} workers.",
        flush=True,
    )

    if not todo:
        if shard_index == 0:
            atomic_write_json(
                status_path,
                {
                    "updated_at": time.time(),
                    "total_movies": total_movies,
                    "ready_clips": len(existing),
                    "encoding_active": False,
                    "clips_per_sec": 0.0,
                    "failed": 0,
                },
            )
        return

    if shard_index == 0:
        atomic_write_json(
            status_path,
            {
                "updated_at": time.time(),
                "total_movies": total_movies,
                "ready_clips": len(existing),
                "encoding_active": True,
                "clips_per_sec": 0.0,
                "failed": 0,
            },
        )

    worker_tag = f"s{shard_index}_{os.getpid()}"
    completed = 0
    failed = 0
    t0 = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(encode_one_clip, ffmpeg_bin, m, clips_dir, worker_tag, cfg): m
            for m in todo
        }
        for fut in concurrent.futures.as_completed(future_map):
            key, ok, msg = fut.result()
            if ok:
                completed += 1
            else:
                failed += 1
                print(f"[WARN] Clip {key} failed: {msg}", flush=True)

            done_count = completed + failed
            if done_count == 1 or done_count % 10 == 0 or done_count == len(todo):
                elapsed = max(0.1, time.time() - t0)
                rate = completed / elapsed
                ready_now = len([1 for p in clips_dir.glob("*.mp4") if not p.name.endswith(".tmp.mp4")])
                print(
                    f"[Encoder] +{completed} encoded ({failed} failed) | "
                    f"Ready on disk: {ready_now}/{total_movies} | {rate:.2f} clips/sec",
                    flush=True,
                )
                if shard_index == 0:
                    atomic_write_json(
                        status_path,
                        {
                            "updated_at": time.time(),
                            "total_movies": total_movies,
                            "ready_clips": ready_now,
                            "encoding_active": done_count < len(todo),
                            "clips_per_sec": round(rate, 2),
                            "failed": failed,
                        },
                    )


def main() -> None:
    cfg = load_config()
    parser = argparse.ArgumentParser(
        description="Local Media Plex, Emby & Jellyfin VideoWall Clip Sync & Encoder"
    )
    parser.add_argument("--wall-dir", default=str(cfg.wall_dir), help="Directory for clips/ and catalog.json")
    parser.add_argument("--workers", type=int, default=max(2, min(8, (os.cpu_count() or 4) // 2)))
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-total", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0, help="Encode at most N movies (for quick testing)")
    parser.add_argument("--catalog-only", action="store_true", help="Sync catalog & collection order without encoding")
    parser.add_argument("--daemon", action="store_true", help="Continuously watch library for newly added movies")
    parser.add_argument("--interval", type=int, default=900, help="Daemon poll interval in seconds (default: 900)")
    args = parser.parse_args()

    wall_dir = Path(args.wall_dir).resolve()
    wall_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg_bin = "ffmpeg" if args.catalog_only else find_ffmpeg()
    router = MediaRouter(cfg)
    print(f"[Encoder] Using FFmpeg: {ffmpeg_bin} | Data Dir: {wall_dir}", flush=True)

    while True:
        try:
            run_sync_pass(
                cfg=cfg,
                router=router,
                wall_dir=wall_dir,
                ffmpeg_bin=ffmpeg_bin,
                workers=max(1, args.workers),
                shard_index=args.shard_index,
                shard_total=max(1, args.shard_total),
                limit=args.limit,
                catalog_only=args.catalog_only,
            )
        except Exception as exc:
            print(f"[ERROR] Sync pass failed: {exc}", file=sys.stderr, flush=True)

        if not args.daemon:
            break
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    main()
