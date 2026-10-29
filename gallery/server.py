#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""相册浏览界面（本地 HTTP 服务，仅用标准库）。

    python gallery/server.py --port 8765
    # 然后浏览器打开 http://127.0.0.1:8765/

数据来源：autohome-backup/data/autohome.db + data/img/ 下的本地图片。
接口默认只读；唯一会动手的是 /api/job —— 由界面上的"爬取"按钮触发，
在后台起一个 `python backup.py ...` 子进程，实时把输出吐给前端。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
DEFAULT_DATA = BASE / "data"
INDEX = HERE / "index.html"

# 单次任务上限，与 backup.py 里的 MAX_BATCH 保持一致
MAX_BATCH = 1000

# 站点上 >500px 的缩略图命名规律：500_xxx.jpg -> xxx.jpg 通常可拿到大图，
# 但不同相册 CDN 规律不一致，这里不擅自改写，原样展示。


# --------------------------------------------------------------------------- #
# 后台任务（界面上那个"爬取"按钮）
# --------------------------------------------------------------------------- #
JOB = {"running": False, "kind": None, "started": 0.0, "ended": 0.0, "code": None,
       "lines": [], "cmd": [], "proc": None, "done": 0, "total": 0}
JOB_LOCK = threading.Lock()


def job_snapshot() -> dict:
    with JOB_LOCK:
        return {
            "running": JOB["running"], "kind": JOB["kind"],
            "started": JOB["started"], "ended": JOB["ended"], "code": JOB["code"],
            "done": JOB["done"], "total": JOB["total"],
            "cmd": JOB["cmd"], "lines": JOB["lines"][-120:],
            "elapsed": round((JOB["ended"] or time.time()) - JOB["started"], 1)
            if JOB["started"] else 0,
        }


def _job_reader(proc: subprocess.Popen) -> None:
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip("\r\n")
            with JOB_LOCK:
                JOB["lines"].append(line)
                if len(JOB["lines"]) > 600:
                    del JOB["lines"][:200]
                m = re.search(r"\[(\d+)/(\d+)\]", line)
                if m:
                    JOB["done"], JOB["total"] = int(m.group(1)), int(m.group(2))
    finally:
        code = proc.wait()
        with JOB_LOCK:
            JOB.update(running=False, code=code, ended=time.time(), proc=None)
            JOB["lines"].append(f"=== 结束，退出码 {code} ===")


def start_job(args: list[str], kind: str) -> dict:
    with JOB_LOCK:
        if JOB["running"]:
            return {"error": "已有任务在跑，先等它结束或点停止"}
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONWARNINGS"] = "ignore"
        proc = subprocess.Popen(
            [sys.executable, "-u", str(BASE / "backup.py")] + args,
            cwd=str(BASE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", env=env)
        JOB.update(running=True, kind=kind, started=time.time(), ended=0.0,
                   code=None, lines=[f"$ python backup.py {' '.join(args)}"],
                   cmd=args, proc=proc, done=0, total=0)
    threading.Thread(target=_job_reader, args=(proc,), daemon=True).start()
    return job_snapshot()


def stop_job() -> dict:
    with JOB_LOCK:
        proc = JOB["proc"]
    if proc and proc.poll() is None:
        proc.terminate()
    return job_snapshot()


# --------------------------------------------------------------------------- #
# 数据
# --------------------------------------------------------------------------- #
def _split_forum(qs) -> tuple | None:
    """把 ?forum=a-100004 解析成 ("a", 100004)；all/空/非法返回 None。"""
    val = (qs.get("forum", [""])[0] or "").strip()
    if not val or val == "all":
        return None
    parts = val.split("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0], int(parts[1])
    return None


def db_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def month_bound(v: str, hi: bool) -> str:
    """把 2006 / 2006-03 / 2006-03-01 归一成 YYYY-MM 边界。"""
    v = (v or "").strip().replace("/", "-")
    if not v:
        return ""
    parts = v.split("-")
    if len(parts) == 1:
        return f"{parts[0]}-{'12' if hi else '01'}"
    if len(parts) >= 2:
        return f"{parts[0]}-{int(parts[1]):02d}"
    return ""


def media_rel(topic_id: int, local_path: str | None, url: str) -> str:
    """优先本地文件；没有本地文件时返回远端直链（前端照常显示）。"""
    if local_path:
        return "/media/" + urllib.parse.quote(local_path.replace("\\", "/"))
    return url


def dedupe_images(rows: list[dict]) -> list[dict]:
    """同一张图（sha1 相同，或 URL 相同）在相册里只留一张，记下出现次数与楼层。"""
    seen: dict = {}
    out: list = []
    for i in rows:
        key = i.get("sha1") or i.get("url")
        if key in seen:
            seen[key]["dup"] += 1
            seen[key]["floors"].append(i.get("floor_no"))
            continue
        i["dup"] = 1
        i["floors"] = [i.get("floor_no")]
        seen[key] = i
        out.append(i)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "AutohomeGallery/1.1"
    data_dir: Path = DEFAULT_DATA

    # ---------------- 基础输出 ----------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def log_message(self, fmt, *args):  # 安静一点
        return

    # ---------------- 路由 ----------------
    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/job":
                self.api_start_job(qs)
            elif path == "/api/job/stop":
                self._json(stop_job())
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)
        db_path = self.data_dir / "autohome.db"
        if not db_path.exists():
            self._json({"error": "database not found: %s" % db_path}, 500)
            return

        try:
            if path in ("/", "/index.html"):
                self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/stats":
                self.api_stats(db_path, qs)
            elif path == "/api/topics":
                self.api_topics(db_path, qs)
            elif path.startswith("/api/topic/"):
                self.api_topic(db_path, int(path.rsplit("/", 1)[1]), qs)
            elif path == "/api/wayback":
                self.api_wayback(db_path, qs)
            elif path == "/api/export":
                self.api_export(db_path, qs)
            elif path == "/api/resume":
                self.api_resume(db_path)
            elif path == "/api/forums":
                self.api_forums(db_path)
            elif path == "/api/album_zip":
                self.api_album_zip(db_path, qs)
            elif path == "/api/job":
                self._json(job_snapshot())
            elif path.startswith("/media/"):
                self.serve_media(path[len("/media/"):])
            else:
                self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---------------- 后台任务 ----------------
    def api_start_job(self, qs: dict) -> None:
        kind = (qs.get("kind", ["fetch"])[0] or "fetch").strip()
        try:
            limit = max(1, min(int(qs.get("limit", ["20"])[0]), MAX_BATCH))
        except ValueError:
            limit = 20
        try:
            rps = max(0.5, min(float(qs.get("rps", ["2.5"])[0]), 20))
        except ValueError:
            rps = 2.5
        topic = (qs.get("topic", [""])[0] or "").strip()
        order = (qs.get("order", ["id-asc"])[0] or "id-asc").strip()
        if order not in ("id-asc", "id-desc", "oldest", "newest", "default"):
            order = "id-asc"
        # 游标起点：从哪个 topicId 开始（用于跳过已经遍历过的号段）
        from_id = (qs.get("from_id", [""])[0] or "").strip()
        from_id = int(from_id) if from_id.isdigit() else 0
        # 选中的论坛（"a-100004"）：fetch 只抓该论坛的队列，list 拉该论坛的列表
        forum = (qs.get("forum", [""])[0] or "").strip()
        if forum in ("all", "") or "-" not in forum:
            forum = ""
        pages = (qs.get("pages", ["3"])[0] or "3").strip()

        if kind == "fetch" and topic.isdigit():
            args = ["fetch", "--topic", topic, "--max-pages", "200", "--rps", str(rps)]
        elif kind == "fetch":
            args = ["fetch", "--valid", str(limit),
                    "--limit", str(min(limit * 2, MAX_BATCH * 2)),
                    "--max-attempts", str(min(limit * 4, MAX_BATCH * 4)),
                    "--order", order, "--workers", "2", "--rps", str(rps)]
            if from_id:
                args += ["--from-id", str(from_id)]
            if forum:
                args += ["--only-forum", forum]
        elif kind == "images" and topic.isdigit():
            args = ["images", "--topic", topic, "--retry-errors", "--limit", str(limit),
                    "--workers", "2", "--rps", str(rps)]
        elif kind == "images":
            args = ["images", "--limit", str(limit), "--workers", "2", "--rps", str(rps)]
        elif kind == "list":  # noqa: E501
            if not forum:
                self._json({"error": "列表任务需要先选一个论坛"}, 400)
                return
            ftype, _, fid = forum.partition("-")
            args = ["list", "--forum", ftype, "--fid", fid,
                    "--pages", "1-" + (pages if pages.isdigit() else "3"),
                    "--rps", str(rps)]
            if (qs.get("mode", [""])[0] or "") == "refine":
                args += ["--refine", "--order", "dateline"]
        elif kind == "crawl":
            args = ["crawl", "--from-year", (qs.get("from_year", ["2005"])[0] or "2005"),
                    "--to-year", (qs.get("to_year", ["2013"])[0] or "2013"),
                    "--rounds", "5", "--batch", str(min(limit, MAX_BATCH)),
                    "--workers", "2", "--rps", str(rps), "--cdx-pages", "2"]
            if forum:
                ftype, _, fid = forum.partition("-")
                args += ["--forum", ftype, "--fid", fid]
        else:
            self._json({"error": f"未知任务类型: {kind}"}, 400)
            return
        snap = start_job(args, kind)
        if forum:
            snap["forum"] = forum
        self._json(snap)

    # ---------------- 接口 ----------------
    def api_stats(self, db_path: Path, qs: dict | None = None) -> None:
        c = db_conn(db_path)
        try:
            qs = qs or {}
            one = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
            nf = "COALESCE(is_face,0)=0"
            f_where, f_vals = "", ()
            forum = _split_forum(qs)
            if forum:
                f_where = " AND forum_type=? AND forum_id=?"
                f_vals = forum
            stats = {
                "topics": one("SELECT COUNT(*) FROM topics"),
                "topics_done": one("SELECT COUNT(*) FROM topics WHERE status IN ('done','deleted')"),
                "floors": one("SELECT COUNT(*) FROM floors"),
                "images": one(f"SELECT COUNT(*) FROM images WHERE {nf}"),
                "images_uniq": one("SELECT COUNT(*) FROM (SELECT DISTINCT topic_id, "
                                   f"COALESCE(sha1,url) FROM images WHERE {nf})"),
                "images_done": one(f"SELECT COUNT(*) FROM images WHERE status='done' AND {nf}"),
                "faces": one("SELECT COUNT(*) FROM images WHERE COALESCE(is_face,0)=1"),
                "bytes": one(f"SELECT COALESCE(SUM(bytes),0) FROM images "
                             f"WHERE status='done' AND {nf}"),
                "topics_with_img": one(f"SELECT COUNT(DISTINCT topic_id) FROM images WHERE {nf}"),
                "forums_total": one("SELECT COUNT(*) FROM forums"),
                "forums_used": one("SELECT COUNT(DISTINCT forum_type||'-'||forum_id) "
                                   "FROM topics WHERE forum_type IS NOT NULL"),
                "timeline": [dict(r) for r in c.execute(
                    "SELECT substr(publish_at,1,4) y, COUNT(*) n FROM topics "
                    "WHERE publish_at IS NOT NULL" + f_where + " GROUP BY y ORDER BY y",
                    f_vals)],
                "months": [dict(r) for r in c.execute(
                    "SELECT substr(publish_at,1,7) m, COUNT(*) n FROM topics "
                    "WHERE publish_at IS NOT NULL" + f_where + " GROUP BY m ORDER BY m",
                    f_vals)],
                "wayback": {r["status"]: r["c"] for r in c.execute(
                    "SELECT status, COUNT(*) c FROM wayback GROUP BY status")},
            }
        finally:
            c.close()
        self._json(stats)

    def api_resume(self, db_path: Path) -> None:
        """算出“应该从哪个 topicId 续跑”。

        topicId 全站递增、与时间同序，所以队列里最小的待抓 id 就是“最早还没备份的帖子”。
        队列抓空后则接着扫新号（已扫过的最大 id + 1）。
        """
        c = db_conn(db_path)
        try:
            qmin, qmax, qn = c.execute(
                "SELECT MIN(topic_id), MAX(topic_id), COUNT(*) FROM topics "
                "WHERE status IN ('new','partial','error')").fetchone()
            done = c.execute("SELECT COUNT(*) FROM topics WHERE status='done'").fetchone()[0]
            deleted = c.execute("SELECT COUNT(*) FROM topics WHERE status='deleted'").fetchone()[0]
            try:
                scanned_max, scanned_n = c.execute(
                    "SELECT MAX(id), COUNT(*) FROM walked").fetchone()
            except sqlite3.OperationalError:  # 老库没有 walked 表
                scanned_max, scanned_n = None, 0
            head = None
            if qn and qmin is not None:
                head = c.execute("SELECT topic_id, title, publish_at FROM topics "
                                 "WHERE topic_id = ?", (qmin,)).fetchone()
                head = dict(head) if head else None
        finally:
            c.close()

        if qn and qmin is not None:
            suggest, reason = qmin, "队列里最早还没抓的帖子"
        elif scanned_max:
            suggest, reason = scanned_max + 1, "队列已空，接着往下扫新号"
        else:
            suggest, reason = 1, "从头开始"
        self._json({
            "suggest": suggest,
            "reason": reason,
            "queue_n": qn,
            "queue_min": qmin,
            "queue_max": qmax,
            "head": head,
            "done": done,
            "deleted": deleted,
            "scanned_max": scanned_max,
            "scanned_n": scanned_n,
        })

    def api_album_zip(self, db_path: Path, qs: dict) -> None:
        """把某篇帖子的本地图片打包成 zip 下载（缺失的写进 manifest）。"""
        try:
            topic_id = int(qs.get("topic", ["0"])[0])
        except ValueError:
            self._json({"error": "topic 参数不对"}, 400)
            return
        c = db_conn(db_path)
        try:
            t = c.execute("SELECT topic_id, title, publish_at FROM topics "
                          "WHERE topic_id = ?", (topic_id,)).fetchone()
            rows = [dict(r) for r in c.execute(
                "SELECT page, floor_no, seq, url, local_path, status FROM images "
                "WHERE topic_id = ? AND COALESCE(is_face,0)=0 ORDER BY page, floor_no, seq",
                (topic_id,))]
        finally:
            c.close()
        if t is None:
            self._json({"error": "没有这篇帖子"}, 404)
            return
        if not rows:
            self._json({"error": "这篇帖子没有图片（或全是表情）"}, 409)
            return

        have, miss = [], []
        for r in rows:
            lp = (r.get("local_path") or "").strip()
            p = (self.data_dir / lp) if lp else None
            (have if (p and p.is_file()) else miss).append((r, p))

        if not have:
            self._json({"error": "图片还没下载到本地，先点「补缺失图」", "images": len(rows)}, 409)
            return

        def safe(s: str) -> str:
            return re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (s or "").strip())[:60] or "album"

        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            for r, p in have:
                arc = "%03d-%03d-%02d%s" % (int(r["page"] or 1), int(r["floor_no"] or 0),
                                            int(r["seq"] or 0), p.suffix or ".jpg")
                z.write(p, arc)
            lines = [f"帖子 {topic_id} · {t['title']}",
                     f"发布时间 {t['publish_at'] or '?'}",
                     f"图片共 {len(rows)} 张，已打包 {len(have)} 张，缺失 {len(miss)} 张", ""]
            for r in rows:
                ok = any(r is h[0] for h in have)
                lines.append("%s 第%s楼 #%s  %s" % ("OK  " if ok else "MISS",
                                                    r["floor_no"], r["seq"], r["url"]))
            z.writestr("manifest.txt", "\n".join(lines))

        data = buf.getvalue()
        buf.close()
        fname = "album-%d-%s.zip" % (topic_id, safe(t["title"]))
        self._send(200, data, "application/zip", {
            "Content-Disposition": "attachment; filename=album-%d.zip; filename*=UTF-8''%s"
                                   % (topic_id, urllib.parse.quote(fname)),
        })

    def api_topics(self, db_path: Path, qs: dict) -> None:
        q = (qs.get("q", [""])[0] or "").strip()
        sort = qs.get("sort", ["new"])[0]
        only_img = qs.get("only_img", [""])[0] in ("1", "true", "yes")
        include_all = qs.get("all", [""])[0] in ("1", "true", "yes")
        include_deleted = qs.get("deleted", [""])[0] in ("1", "true", "yes")
        try:
            limit = min(int(qs.get("limit", ["60"])[0]), 300)
        except ValueError:
            limit = 60
        try:
            offset = max(int(qs.get("offset", ["0"])[0]), 0)
        except ValueError:
            offset = 0

        where, params = [], []
        df, dt = month_bound(qs.get("from", [""])[0], hi=False), \
            month_bound(qs.get("to", [""])[0], hi=True)
        if df:
            where.append("substr(t.publish_at,1,7) >= ?")
            params.append(df)
        if dt:
            where.append("substr(t.publish_at,1,7) <= ?")
            params.append(dt)
        forum = _split_forum(qs)
        if forum:
            where.append("t.forum_type = ? AND t.forum_id = ?")
            params += [forum[0], forum[1]]
        if q:
            where.append("(t.title LIKE ? OR t.topic_id = ?)")
            params += [f"%{q}%", int(q) if q.isdigit() else -1]
        if only_img:
            where.append("t.topic_id IN (SELECT topic_id FROM images "
                         "WHERE COALESCE(is_face,0)=0)")
        if not include_all:
            where.append("t.topic_id IN (SELECT topic_id FROM floors)")
        if not include_deleted:
            where.append("t.status != 'deleted'")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        if sort == "old":
            order = "t.publish_at ASC"
        elif sort == "fetched":
            order = "t.fetched_at DESC"
        else:
            order = "t.publish_at DESC"

        nf = "AND COALESCE(i.is_face,0)=0"
        sql = f"""
            SELECT t.topic_id, t.title, t.publish_at, t.reply_count, t.author_name,
                   t.status, t.source, t.pages_total, t.hash, t.fetched_at,
                   t.forum_type, t.forum_id,
                   (SELECT f.name FROM forums f WHERE f.bbs_type=t.forum_type
                      AND f.bbs_id=t.forum_id) AS forum_name,
                   (SELECT COUNT(DISTINCT COALESCE(i.sha1,i.url)) FROM images i
                     WHERE i.topic_id = t.topic_id {nf}) AS img_total,
                   (SELECT COUNT(DISTINCT COALESCE(i.sha1,i.url)) FROM images i
                     WHERE i.topic_id = t.topic_id AND i.status='done' {nf}) AS img_done,
                   (SELECT COUNT(*) FROM floors f WHERE f.topic_id = t.topic_id) AS floors,
                   (SELECT i.local_path FROM images i WHERE i.topic_id = t.topic_id {nf}
                      AND i.status='done' AND i.local_path IS NOT NULL
                      ORDER BY i.page, i.seq LIMIT 1) AS cover_path,
                   (SELECT i.url FROM images i WHERE i.topic_id = t.topic_id {nf}
                      AND i.status IN ('done','new')
                      ORDER BY i.page, i.seq LIMIT 1) AS cover_url
            FROM topics t {clause}
            ORDER BY {order}, t.topic_id LIMIT ? OFFSET ?
        """
        c = db_conn(db_path)
        try:
            rows = [dict(r) for r in c.execute(sql, params + [limit, offset])]
            total = c.execute(f"SELECT COUNT(*) FROM topics t {clause}", params).fetchone()[0]
        finally:
            c.close()
        for r in rows:
            r["cover"] = media_rel(r["topic_id"], r.pop("cover_path"), r.pop("cover_url") or "")
        self._json({"total": total, "offset": offset, "limit": limit, "items": rows})

    def api_forums(self, db_path: Path) -> None:
        """论坛层：目录（2000+ 个）+ 本地已有数据的统计。"""
        c = db_conn(db_path)
        try:
            rows = [dict(r) for r in c.execute("""
                SELECT f.bbs_type, f.bbs_id, f.name, f.topic_total, f.probed_at,
                       (SELECT COUNT(*) FROM topics t WHERE t.forum_type=f.bbs_type
                          AND t.forum_id=f.bbs_id) AS n,
                       (SELECT COUNT(*) FROM topics t WHERE t.forum_type=f.bbs_type
                          AND t.forum_id=f.bbs_id AND t.status='done') AS n_done,
                       (SELECT MIN(t.publish_at) FROM topics t WHERE t.forum_type=f.bbs_type
                          AND t.forum_id=f.bbs_id) AS earliest,
                       (SELECT MAX(t.publish_at) FROM topics t WHERE t.forum_type=f.bbs_type
                          AND t.forum_id=f.bbs_id) AS latest
                FROM forums f
                ORDER BY (n > 0) DESC, n DESC, f.bbs_type, f.bbs_id
            """)]
        finally:
            c.close()
        self._json({"total": len(rows), "items": rows})

    def api_topic(self, db_path: Path, topic_id: int, qs: dict | None = None) -> None:
        qs = qs or {}
        with_faces = (qs.get("faces", [""])[0] or "") in ("1", "true", "yes")
        c = db_conn(db_path)
        try:
            t = c.execute("SELECT * FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
            if not t:
                self._json({"error": "topic not found"}, 404)
                return
            topic = dict(t)
            floors = []
            for f in c.execute("SELECT * FROM floors WHERE topic_id=? ORDER BY page, floor_no",
                               (topic_id,)):
                d = dict(f)
                d["images"] = json.loads(d["images"] or "[]")
                floors.append(d)
            rows = []
            for i in c.execute("SELECT * FROM images WHERE topic_id=? ORDER BY page, seq",
                               (topic_id,)):
                d = dict(i)
                if not with_faces and d.get("is_face"):
                    continue
                d["src"] = media_rel(topic_id, d["local_path"], d["url"])
                rows.append(d)
            imgs = dedupe_images(rows)
            wb = c.execute("SELECT * FROM wayback WHERE topic_id=?", (topic_id,)).fetchone()
        finally:
            c.close()
        self._json({"topic": topic, "floors": floors, "images": imgs,
                    "image_rows": len(rows), "wayback": dict(wb) if wb else None})

    def api_wayback(self, db_path: Path, qs: dict) -> None:
        c = db_conn(db_path)
        try:
            rows = [dict(r) for r in c.execute(
                "SELECT w.*, t.title FROM wayback w LEFT JOIN topics t USING(topic_id) "
                "ORDER BY w.checked_at DESC LIMIT 500")]
        finally:
            c.close()
        self._json({"items": rows})

    # ---------------- 集合导出 ----------------
    def api_export(self, db_path: Path, qs: dict) -> None:
        """把筛选结果（或指定 topicId 集合）打成 zip 直接下载。"""
        import tempfile  # noqa: F401

        sys.path.insert(0, str(BASE))
        import exportlib

        ids_raw = (qs.get("ids", [""])[0] or "").strip()
        ids = [int(x) for x in ids_raw.replace(" ", ",").split(",") if x.isdigit()]
        df = (qs.get("from", [""])[0] or "").strip()
        dt = (qs.get("to", [""])[0] or "").strip()
        raw = (qs.get("raw", [""])[0] or "") in ("1", "true")
        only_done = (qs.get("only_done", [""])[0] or "") in ("1", "true")
        try:
            limit = max(1, min(int(qs.get("limit", ["300"])[0]), exportlib.MAX_TOPICS))
        except ValueError:
            limit = 300

        conn = sqlite3.connect(db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            topics = exportlib.query_topics(conn, ids=ids or None, date_from=df or None,
                                            date_to=dt or None, limit=limit, order="asc",
                                            only_done=only_done,
                                            forum=(qs.get("forum", [""])[0] or None))
            if not ((qs.get("deleted", [""])[0] or "") in ("1", "true")):
                topics = [t for t in topics if t["status"] != "deleted"]
            if (qs.get("only_img", [""])[0] or "") in ("1", "true") and topics:
                tids = [t["topic_id"] for t in topics]
                have = {r[0] for r in conn.execute(
                    "SELECT DISTINCT topic_id FROM images WHERE topic_id IN (%s) "
                    "AND COALESCE(is_face,0)=0"
                    % ",".join("?" * len(tids)), tids)}
                topics = [t for t in topics if t["topic_id"] in have]
            if not topics:
                self._json({"error": "没有匹配的帖子", "hint": "先抓取，或放宽时间范围"}, 404)
                return
            tmpdir = self.data_dir / ".tmp"
            tmpdir.mkdir(exist_ok=True)
            out = tmpdir / f"export-{os.getpid()}-{time.time_ns()}.zip"
            stats = exportlib.build_zip(conn, self.data_dir, out, topics,
                                        include_raw=raw)
            with open(out, "rb") as fh:
                data = fh.read()
            try:
                out.unlink()
            except OSError:
                pass
        finally:
            conn.close()

        name = exportlib.default_name(df or None, dt or None, ids or None)
        ascii_name = "autohome-backup-%d.zip" % len(topics)
        disp = (f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{urllib.parse.quote(name)}")
        self._send(200, data, "application/zip", {
            "Content-Disposition": disp,
            "X-Export-Stats": json.dumps(stats, ensure_ascii=False),
        })

    # ---------------- 静态图片 ----------------
    def serve_media(self, rel: str) -> None:
        rel = rel.replace("\\", "/").lstrip("/")
        target = (self.data_dir / rel).resolve()
        try:
            target.relative_to(self.data_dir.resolve())
        except ValueError:
            self._json({"error": "forbidden"}, 403)
            return
        if not target.is_file():
            self._json({"error": "missing", "path": rel}, 404)
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        data = target.read_bytes()
        self._send(200, data, ctype, {"Cache-Control": "public, max-age=86400"})


def main() -> None:
    ap = argparse.ArgumentParser(description="汽车之家备份 · 相册浏览界面")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA))
    args = ap.parse_args()

    Handler.data_dir = Path(args.data_dir).resolve()
    if not (Handler.data_dir / "autohome.db").exists():
        print(f"[warn] 未找到数据库：{Handler.data_dir / 'autohome.db'}")
        print("       先跑 python backup.py init / list / fetch")
    class GalleryServer(ThreadingHTTPServer):
        # Windows 的 SO_REUSEADDR 会让第二个进程也能绑上同一端口并抢走请求，
        # 必须关掉；POSIX 下保留（方便快速重启）。
        allow_reuse_address = (os.name != "nt")
        daemon_threads = True

    # 端口被占：如果是本程序自己的实例在跑，就别再起第二个
    try:
        srv = GalleryServer((args.host, args.port), Handler)
    except OSError as exc:
        import urllib.request
        try:
            with urllib.request.urlopen(
                    f"http://{args.host}:{args.port}/api/stats", timeout=3) as r:
                alive = r.status == 200
        except Exception:                                    # noqa: BLE001
            alive = False
        if alive:
            print(f"[已在运行] 相册界面: http://{args.host}:{args.port}/")
            print("           要重启就先关掉旧进程（或点 停止相册.bat）")
            return
        raise SystemExit(f"端口 {args.port} 被占用且不是本程序：{exc}")

    pid_file = Path(__file__).with_name("server.pid")
    try:
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pid_file = None
    print(f"相册界面: http://{args.host}:{args.port}/")
    print(f"数据目录: {Handler.data_dir}")
    print(f"进程号: {os.getpid()}（关闭窗口会停；想长期挂用 后台运行相册.bat）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        try:
            if pid_file:
                pid_file.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
