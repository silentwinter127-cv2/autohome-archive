#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集合导出：把一批帖子（楼层正文 + 图片 + 原始 HTML）打包成 zip。

被两处复用：
  - 命令行  python backup.py export --from 2006-01 --to 2009-12 --out xxx.zip
  - 网页     GET /api/export?ids=...  或  ?from=2011-01&to=2011-12

zip 结构：
  manifest.json          帖子和图片清单（含缺失项）
  index.html             离线可打开的相册首页
  topic_<id>.html        单帖静态页（楼层正文 + 内嵌图片）
  posts/<id>.txt         纯文本楼层（便于 grep / 语料）
  img/<id>/<page>-<seq>  本地已下载的图片
  raw/<id>/*.html.gz     原始 HTML（可选 --raw / raw=1）
"""
from __future__ import annotations

import html
import json
import os
import sqlite3
import time
import zipfile
from pathlib import Path

MAX_TOPICS = 800


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def _norm_from(v: str | None) -> str | None:
    v = (v or "").strip()
    if not v:
        return None
    if len(v) == 4:
        return f"{v}-01-01"
    if len(v) == 7:
        return f"{v}-01"
    return v.replace("/", "-")


def _norm_to(v: str | None) -> str | None:
    v = (v or "").strip()
    if not v:
        return None
    if len(v) == 4:
        return f"{v}-12-31"
    if len(v) == 7:
        return f"{v}-31"
    return v.replace("/", "-")


def query_topics(conn: sqlite3.Connection, ids=None, date_from=None, date_to=None,
                 limit: int = MAX_TOPICS, order: str = "asc", only_done: bool = False,
                 forum: str | None = None) -> list:
    where, params = [], []
    if forum and forum != "all":
        ftype, _, fid = str(forum).partition("-")
        if fid.isdigit():
            where.append("forum_type = ? AND forum_id = ?")
            params += [ftype, int(fid)]
    if ids:
        ids = [int(i) for i in ids][:limit]
        where.append("topic_id IN (%s)" % ",".join("?" * len(ids)))
        params += ids
    else:
        df, dt = _norm_from(date_from), _norm_to(date_to)
        if df:
            where.append("substr(publish_at,1,10) >= ?")
            params.append(df)
        if dt:
            where.append("substr(publish_at,1,10) <= ?")
            params.append(dt)
        where.append("publish_at IS NOT NULL")
    if only_done:
        where.append("status IN ('done','partial')")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    direction = "DESC" if order == "desc" else "ASC"
    sql = (f"SELECT * FROM topics {clause} ORDER BY publish_at {direction}, topic_id {direction} "
           f"LIMIT ?")
    return list(conn.execute(sql, params + [limit]))


# --------------------------------------------------------------------------- #
# 单帖静态页
# --------------------------------------------------------------------------- #
CSS = """
*{box-sizing:border-box}body{margin:0;background:#0f1115;color:#e6e8ee;
 font:14px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;padding:24px}
a{color:#ff9a6c;text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:18px;margin:0 0 6px}h2{font-size:16px}
.meta{color:#9aa3b2;font-size:12px;margin-bottom:18px}
.album{display:grid;gap:8px;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));margin:18px 0}
.album img{width:100%;aspect-ratio:1/1;object-fit:cover;border-radius:8px;background:#1c212b}
.album a{position:relative;display:block}
.album .dup{position:absolute;right:6px;bottom:6px;background:rgba(0,0,0,.66);color:#ffcf9e;
 border-radius:6px;padding:0 6px;font-size:11px;font-weight:600}
.floor{background:#171a21;border:1px solid #2a2f3a;border-radius:10px;padding:12px 14px;margin:12px 0}
.floor .fh{font-size:12px;color:#9aa3b2;margin-bottom:6px}
.floor .fh b{color:#e6e8ee}
.floor .txt{white-space:pre-wrap;word-break:break-word}
.floor .ims{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.floor .ims img{height:72px;width:72px;object-fit:cover;border-radius:6px}
.missing{color:#e07b7b;font-size:12px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(220px,1fr))}
.card{background:#171a21;border:1px solid #2a2f3a;border-radius:12px;overflow:hidden}
.card img{width:100%;aspect-ratio:4/3;object-fit:cover;display:block;background:#1c212b}
.card .ph{aspect-ratio:4/3;display:grid;place-items:center;color:#4b5563;font-size:28px;
 background:#1c212b}
.card .b{padding:8px 10px}
.card .t{font-size:13px;line-height:1.4;max-height:2.8em;overflow:hidden}
.card .m{color:#9aa3b2;font-size:11.5px;margin-top:4px}
"""


def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _img_src(path: str | None, url: str) -> str:
    if path:
        return path.replace("\\", "/")
    return url or ""


FACE_MARKERS = ("/album/smiles/", "/club/images/face/", "/images/face/",
                "/album/editor/smiles", "/face/emotion")


def _is_face(url) -> bool:
    u = (url or "").lower()
    return any(m in u for m in FACE_MARKERS)


def unique_images(images) -> list:
    """按内容指纹去重（sha1，缺则 url）：同图只留一张，记下出现次数与楼层。"""
    seen: dict = {}
    out: list = []
    for row in images:
        d = dict(row)
        if _is_face(d.get("url")):
            continue
        key = d.get("sha1") or d.get("url")
        if key in seen:
            seen[key]["dup"] = seen[key].get("dup", 1) + 1
            seen[key]["floors"].append(d.get("floor_no"))
            continue
        d["dup"] = 1
        d["floors"] = [d.get("floor_no")]
        seen[key] = d
        out.append(d)
    return out


def topic_html(topic, floors, images) -> str:
    t = dict(topic)
    rows = []
    for f in floors:
        d = dict(f)
        try:
            imgs = json.loads(d.get("images") or "[]")
        except Exception:  # noqa: BLE001
            imgs = []
        imhtml = "".join(
            f'<a href="{_esc(u)}" target="_blank"><img loading="lazy" src="{_esc(u)}"></a>'
            for u in imgs)
        who = "楼主" if d.get("floor_no") == 0 else f"@{d.get('floor_no')} 楼"
        rows.append(
            f'<div class="floor"><div class="fh"><b>{who}</b> · {_esc(d.get("author"))} · '
            f'{_esc(d.get("posted_at"))}</div>'
            f'<div class="txt">{_esc(d.get("text"))}</div>'
            + (f'<div class="ims">{imhtml}</div>' if imhtml else "")
            + "</div>")

    local = [i for i in images if i["status"] == "done" and i["local_path"]]
    missing = [i for i in images if not (i["status"] == "done" and i["local_path"])]
    cells = []
    for i in local:
        src = _esc(_img_src(i["local_path"], i["url"]))
        dup = i.get("dup", 1)
        tag = f'<b class="dup">×{dup}</b>' if dup > 1 else ""
        cells.append(f'<a href="{src}" target="_blank" title="出现 {dup} 次">'
                     f'<img loading="lazy" src="{src}">{tag}</a>')
    grid = "".join(cells)
    miss = ""
    if missing:
        miss = ('<p class="missing">另有 %d 张图片本地缺失（源站已删且 Wayback 无快照）：%s</p>'
                % (len(missing), _esc(", ".join(i["url"] for i in missing[:8]))))

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{_esc(t.get('title'))}</title><style>{CSS}</style></head><body>
<p><a href="index.html">← 返回合集</a></p>
<h1>{_esc(t.get('title'))}</h1>
<div class="meta">帖子 ID {t.get('topic_id')} · 发布 {_esc(t.get('publish_at'))} ·
 作者 {_esc(t.get('author_name'))} · {len(floors)} 楼 · {len(images)} 图</div>
{'<div class="album">' + grid + '</div>' if grid else ''}
{miss}
<h2>楼层正文</h2>
{''.join(rows)}
</body></html>"""


def index_html(topics, stats) -> str:
    cards = []
    for t in topics:
        d = dict(t)
        cover = d.get("_cover") or ""
        head = (f'<img loading="lazy" src="{_esc(cover)}">' if cover
                else '<div class="ph">▦</div>')
        cards.append(
            f'<a class="card" href="topic_{d["topic_id"]}.html">' + head
            + f'<div class="b"><div class="t">{_esc(d.get("title"))}</div>'
              f'<div class="m">{_esc((d.get("publish_at") or "")[:10])} · '
              f'{d.get("_imgs", 0)} 图 · {d.get("_floors", 0)} 楼</div></div></a>')
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>汽车之家论坛备份 · 合集</title><style>{CSS}</style></head><body>
<h1>汽车之家论坛备份 · 合集</h1>
<div class="meta">导出时间 {time.strftime('%Y-%m-%d %H:%M:%S')} ·
 {stats['topics']} 帖 / {stats['floors']} 楼 / {stats['images']} 图
 （重复 {stats.get('dup', 0)} 次已合并，缺失 {stats['missing']}，原始 HTML {stats['raw']} 页）</div>
<div class="grid">{''.join(cards)}</div>
</body></html>"""


# --------------------------------------------------------------------------- #
# 打包
# --------------------------------------------------------------------------- #
def build_zip(conn: sqlite3.Connection, data_dir: Path, out_path: Path,
              topics: list, include_raw: bool = False) -> dict:
    data_dir = Path(data_dir)
    stats = {"topics": 0, "floors": 0, "images": 0, "dup": 0, "missing": 0,
             "raw": 0, "bytes": 0}
    enriched, manifest, texts = [], [], []
    text_map: dict = {}
    uniq_map: dict = {}

    for t in topics:
        tid = t["topic_id"]
        floors = list(conn.execute(
            "SELECT * FROM floors WHERE topic_id=? ORDER BY page, floor_no", (tid,)))
        imgs = unique_images(conn.execute(
            "SELECT * FROM images WHERE topic_id=? ORDER BY page, seq", (tid,)).fetchall())
        uniq_map[tid] = imgs
        d = dict(t)
        d["_floors"] = len(floors)
        d["_imgs"] = len(imgs)
        done = [i for i in imgs if i["status"] == "done" and i["local_path"]]
        d["_cover"] = (done[0]["local_path"].replace("\\", "/") if done
                       else (imgs[0]["url"] if imgs else ""))
        enriched.append(d)

        stats["topics"] += 1
        stats["floors"] += len(floors)
        stats["images"] += len(imgs)
        stats["dup"] += sum(i.get("dup", 1) - 1 for i in imgs)
        stats["missing"] += len(imgs) - len(done)

        plain = [f"# {t['title']}", f"# id={tid}  publish={t['publish_at']}  "
                 f"author={t['author_name']}", ""]
        for f in floors:
            plain.append(f"[{f['floor_no']}] {f['author']} {f['posted_at']}")
            plain.append(f["text"] or "")
            for u in json.loads(f["images"] or "[]"):
                plain.append(f"  <img> {u}")
            plain.append("")
        texts.append((tid, "\n".join(plain)))
        text_map[tid] = "\n".join(plain)

        manifest.append({
            "topic_id": tid, "title": t["title"], "publish_at": t["publish_at"],
            "author": t["author_name"], "url": t["url"], "hash": t["hash"],
            "floors": len(floors), "images_total": len(imgs),
            "images_dup": sum(i.get("dup", 1) - 1 for i in imgs),
            "images_local": len(done),
            "images_missing": [i["url"] for i in imgs if i not in done][:50],
        })

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        z.writestr("index.html", index_html(enriched, stats))

        for t in topics:
            tid = t["topic_id"]
            floors = list(conn.execute(
                "SELECT * FROM floors WHERE topic_id=? ORDER BY page, floor_no", (tid,)))
            z.writestr(f"topic_{tid}.html", topic_html(t, floors, uniq_map.get(tid, [])))
            z.writestr(f"posts/{tid}.txt", text_map.get(tid, ""))

        for t in topics:
            tid = t["topic_id"]
            for i in uniq_map.get(tid, []):
                if i["status"] != "done" or not i["local_path"]:
                    continue
                src = data_dir / i["local_path"]
                if src.is_file():
                    z.write(src, i["local_path"].replace("\\", "/"))
                    stats["bytes"] += src.stat().st_size
            if include_raw:
                for gz in sorted((data_dir / "raw" / str(tid)).glob("*.html.gz")):
                    z.write(gz, f"raw/{tid}/{gz.name}")
                    stats["raw"] += 1
    return stats


def default_name(date_from=None, date_to=None, ids=None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if ids:
        return f"autohome-{len(list(ids))}帖-{stamp}.zip"
    lo = (date_from or "开始").replace("-", "")[:8]
    hi = (date_to or "现在").replace("-", "")[:8]
    return f"autohome-{lo}-{hi}-{stamp}.zip"
