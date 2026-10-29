#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
汽车之家论坛（club.autohome.com.cn）备份工具
================================================

子命令
------
  init                 初始化数据库与目录
  list                 枚举旧版列表页（/o/bbs/forum-{t}-{fid}-{page}.html），入库帖子元数据
  cdx                  用 Wayback CDX 播种早期帖子（列表最多只能回溯 5 万帖/论坛）
  resolve              对缺少 hash 的帖子走 301 拿规范 URL，顺带确认存活/所属论坛
  fetch                抓取帖子页（优先旧版 SSR 页，含作者名/日期/src9 图片），落盘 + 解析
  images               下载已入队的图片
  wayback              检查 Wayback 是否已归档；缺失则提交 Save Page Now（--submit）
  status               查看进度

设计要点
--------
* 旧版 /o/bbs/thread/{hash}/{id}-{page}.html 是服务端渲染，作者名(xname="uname")、
  日期(xname="date")、正文(xname="content")、图片(src9=) 全在 HTML 里；
  新版 /bbs/thread/... 把作者名交给 JS 异步加载，只留 data-member-id，所以优先旧版。
* 301 是免 hash 的钥匙：/bbs/thread-{t}-{fid}-{id}-1.html 对任意 topicId 都会跳到规范页。
* 失效帖判据：跟随跳转后响应体约 23KB（正常 140-310KB），且无 xname="uname"。

用法示例
--------
  python backup.py init
  python backup.py list  --forum a --fid 100004 --pages 1-5
  python backup.py fetch --limit 20 --workers 6
  python backup.py images --limit 200 --workers 8
  python backup.py wayback --limit 20 --submit
  python backup.py status
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import html as htmllib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
CLUB = "https://club.autohome.com.cn"
WB_AVAILABLE = "https://archive.org/wayback/available"
WB_SAVE = "https://web.archive.org/save/"
WB_CDX = "http://web.archive.org/cdx/search/cdx"

BASE = Path(__file__).resolve().parent
DEFAULT_DATA = BASE / "data"

# 单次任务上限：一条命令/一次点“爬取”最多处理这么多篇（有效帖计数也按此封顶）
MAX_BATCH = 1000

# 图片噪声：表情、占位图、小图标
# 论坛默认表情（smiles）不是"图片"：不计入图数、不下载、不进导出、不进封面。
FACE_MARKERS = ("/album/smiles/", "/club/images/face/", "/images/face/",
                "/album/editor/smiles", "/face/emotion",
                # 论坛默认表情还有这两类：x.autoimg.cn/club/smiles/44.gif
                # 与 www.autoimg.cn/Album/kindeditor/smiles/2.gif
                "/club/smiles/", "/kindeditor/smiles/")


def is_face_url(url: str) -> bool:
    u = (url or "").lower()
    return any(m in u for m in FACE_MARKERS)


IMG_NOISE = ("/album/editor/smiles", "lazyload", "blank.gif", "backquote",
             "/club/common/", "static.", "logo", "spacer", "loading",
             # 站内 UI 素材（不是用户拍的图，很多已 404）
             "/bbs/images/", "/album/userheaders/", "/bbs/script/", "/bbs/css/",
             "static.autohome.com.cn", "s.autoimg.cn")


# --------------------------------------------------------------------------- #
# 基础设施
# --------------------------------------------------------------------------- #
class RateLimiter:
    """线程安全的令牌间隔限速器。"""

    def __init__(self, rps: float):
        self.interval = 1.0 / max(0.001, rps)
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_at)
            self.next_at = at + self.interval
        delay = at - time.monotonic()
        if delay > 0:
            time.sleep(delay)


class Blocked(RuntimeError):
    """站点弹出人机验证（302 -> safety.autohome.com.cn/userverify）。

    这时拿到的一切"没有内容"的响应都不能当作帖子已删。
    """


# 触发人机验证的特征
BLOCK_MARKERS = ("safety.autohome.com.cn", "userverify", "/userverify/index")


def looks_blocked(resp) -> bool:
    loc = (resp.headers.get("Location") or "").lower()
    if any(m in loc for m in BLOCK_MARKERS):
        return True
    url = (getattr(resp, "url", "") or "").lower()
    return any(m in url for m in BLOCK_MARKERS)


class Http:
    """带重试的请求封装；每个线程一个 Session。"""

    def __init__(self, limiter: RateLimiter, timeout: int = 30, retries: int = 3,
                 max_blocks: int = 3):
        self.limiter = limiter
        self.timeout = timeout
        self.retries = retries
        self.max_blocks = max_blocks
        self.blocks = 0
        self._local = threading.local()

    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            # 可选：用浏览器里过完人机验证后的 Cookie 恢复访问
            #   $env:AUTOHOME_COOKIE = "你的 cookie 串"
            ck = os.environ.get("AUTOHOME_COOKIE", "").strip()
            if ck:
                s.headers["Cookie"] = ck
            self._local.session = s
        return s

    def request(self, method: str, url: str, **kw) -> requests.Response:
        kw.setdefault("timeout", self.timeout)
        kw.setdefault("allow_redirects", True)
        last = None
        for attempt in range(self.retries + 1):
            self.limiter.wait()
            try:
                r = self._session().request(method, url, **kw)
                if looks_blocked(r):
                    # 人机验证：退避等待，绝不把它当成“帖子已删”
                    self.blocks += 1
                    if self.blocks > self.max_blocks:
                        raise Blocked("连续触发人机验证，已停止请求")
                    wait = min(180, 20 * (2 ** (self.blocks - 1)))
                    print(f"!! 站点要求人机验证，第 {self.blocks} 次，暂停 {wait}s：{url}")
                    time.sleep(wait)
                    last = r
                    continue
                if r.status_code in (429, 500, 502, 503, 504):
                    last = r
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if self.blocks:
                    print(f"   人机验证已解除（等待后恢复），继续")
                    self.blocks = 0
                return r
            except requests.RequestException as exc:  # 网络抖动
                last = exc
                time.sleep(1.5 * (attempt + 1))
        if isinstance(last, requests.Response):
            return last
        raise RuntimeError(f"请求失败: {url} ({last})")

    def get(self, url: str, **kw) -> requests.Response:
        return self.request("GET", url, **kw)


class Store:
    """SQLite 状态库（WAL，单连接 + 锁）。"""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS topics (
        topic_id     INTEGER PRIMARY KEY,
        forum_type   TEXT,
        forum_id     INTEGER,
        hash         TEXT,
        title        TEXT,
        author_id    INTEGER,
        author_name  TEXT,
        publish_at   TEXT,
        reply_count  INTEGER,
        last_reply_at TEXT,
        source       TEXT,
        url          TEXT,
        pages_total  INTEGER,
        pages_done   INTEGER DEFAULT 0,
        image_count  INTEGER DEFAULT 0,
        status       TEXT DEFAULT 'new',   -- new|partial|done|deleted|error
        fetched_at   TEXT,
        error        TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_topics_date   ON topics(publish_at);
    CREATE INDEX IF NOT EXISTS idx_topics_status ON topics(status);
    CREATE INDEX IF NOT EXISTS idx_topics_forum  ON topics(forum_type, forum_id);

    CREATE TABLE IF NOT EXISTS floors (
        topic_id   INTEGER NOT NULL,
        page       INTEGER NOT NULL,
        floor_no   INTEGER NOT NULL,
        member_id  INTEGER,
        author     TEXT,
        posted_at  TEXT,
        text       TEXT,
        images     TEXT,
        PRIMARY KEY (topic_id, page, floor_no)
    );

    CREATE TABLE IF NOT EXISTS images (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        topic_id   INTEGER NOT NULL,
        page       INTEGER,
        floor_no   INTEGER,
        seq        INTEGER,
        url        TEXT NOT NULL,
        local_path TEXT,
        bytes      INTEGER DEFAULT 0,
        status     TEXT DEFAULT 'new',    -- new|done|face|error
        error      TEXT,
        is_face    INTEGER DEFAULT 0,     -- 论坛默认表情，不算“图片”
        sha1       TEXT,                  -- 内容指纹，用于去重
        UNIQUE(topic_id, url)
    );
    CREATE INDEX IF NOT EXISTS idx_images_status ON images(status);
    CREATE INDEX IF NOT EXISTS idx_images_topic  ON images(topic_id);

    CREATE TABLE IF NOT EXISTS wayback (
        url          TEXT PRIMARY KEY,
        topic_id     INTEGER,
        checked_at   TEXT,
        available    INTEGER,
        snapshot_ts  TEXT,
        submitted_at TEXT,
        status       TEXT,      -- exists|submitted|failed|skipped|error
        http_code    INTEGER,
        note         TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_wayback_status ON wayback(status);

    CREATE TABLE IF NOT EXISTS walked (
        id         INTEGER PRIMARY KEY,
        status     TEXT,
        checked_at TEXT
    );

    CREATE TABLE IF NOT EXISTS forums (
        bbs_type    TEXT,
        bbs_id      INTEGER,
        name        TEXT,
        topic_total INTEGER,    -- 站方口径的“本坛帖子”数（列表页页脚 / API total）
        probed_at   TEXT,
        PRIMARY KEY (bbs_type, bbs_id)
    );
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "raw").mkdir(exist_ok=True)
        (self.data_dir / "img").mkdir(exist_ok=True)
        self.db_path = self.data_dir / "autohome.db"
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(self.SCHEMA)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """老库补列 + 回填表情标记 / 内容指纹（幂等）。"""
        faces = hashed = 0
        with self.lock:
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(images)")}
            if "is_face" not in cols:
                self.conn.execute("ALTER TABLE images ADD COLUMN is_face INTEGER DEFAULT 0")
            if "sha1" not in cols:
                self.conn.execute("ALTER TABLE images ADD COLUMN sha1 TEXT")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_images_sha1 "
                              "ON images(topic_id, sha1)")
            cur = self.conn.execute(
                "UPDATE images SET is_face=1 WHERE COALESCE(is_face,0)=0 AND ("
                + " OR ".join("url LIKE ?" for _ in FACE_MARKERS) + ")",
                tuple("%" + m + "%" for m in FACE_MARKERS))
            faces = cur.rowcount
            pending = self.conn.execute(
                "SELECT id, local_path FROM images WHERE sha1 IS NULL "
                "AND status='done' AND local_path IS NOT NULL").fetchall()
            for row in pending:
                p = self.data_dir / row["local_path"]
                if p.is_file():
                    self.conn.execute(
                        "UPDATE images SET sha1=? WHERE id=?",
                        (hashlib.sha1(p.read_bytes()).hexdigest(), row["id"]))
                    hashed += 1
            self.conn.commit()
        if faces:
            print(f"[migrate] 标记默认表情 {faces} 张")
        if hashed:
            print(f"[migrate] 回填图片指纹 {hashed} 张")

    def exec(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self.lock:
            return list(self.conn.execute(sql, tuple(params)))

    def upsert_topic(self, **kw) -> None:
        cols = ["topic_id", "forum_type", "forum_id", "hash", "title", "author_id",
                "author_name", "publish_at", "reply_count", "last_reply_at",
                "source", "url", "status"]
        vals = [kw.get(c) for c in cols]
        sets = ", ".join(f"{c}=COALESCE(excluded.{c}, topics.{c})" for c in cols if c != "topic_id")
        self.exec(
            f"INSERT INTO topics ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
            f"ON CONFLICT(topic_id) DO UPDATE SET {sets}",
            vals,
        )

    def upsert_forum(self, bbs_type: str, bbs_id: int, name: str | None = None,
                     topic_total: int | None = None, probed: bool = False) -> None:
        self.exec(
            "INSERT INTO forums (bbs_type, bbs_id, name, topic_total, probed_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(bbs_type, bbs_id) DO UPDATE SET "
            "  name=COALESCE(excluded.name, forums.name), "
            "  topic_total=COALESCE(excluded.topic_total, forums.topic_total), "
            "  probed_at=COALESCE(excluded.probed_at, forums.probed_at)",
            (bbs_type, int(bbs_id), name, topic_total,
             now() if (probed or topic_total is not None) else None),
        )

    def close(self) -> None:
        with self.lock:
            self.conn.close()


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def norm_date(s: str) -> str:
    """把 '2006-3-19 21:27:44' / '2026-10-19 13:53' 统一成 'YYYY-MM-DD HH:MM:SS'。"""
    s = (s or "").strip()
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?", s)
    if not m:
        return s
    y, mo, d, h, mi, se = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d} {int(h or 0):02d}:{int(mi or 0):02d}:{int(se or 0):02d}"


def clean_text(fragment: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", fragment)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|li|h\d|tr)>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    s = htmllib.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def norm_img(url: str) -> str:
    url = htmllib.unescape(url).strip().strip('"').strip("'")
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://club.autohome.com.cn" + url
    return url.split("?")[0] if "autoimg.cn" in url else url


def is_real_image(url: str) -> bool:
    low = url.lower()
    if not low.startswith("http"):
        return False
    if any(n in low for n in IMG_NOISE):
        return False
    return bool(re.search(r"\.(jpe?g|png|gif|webp|bmp)$", low))


LIST_ROW = re.compile(r'<dl\s+class="list_dl"\s+lang="([^"]*)"(.*?)</dl>', re.S)


def parse_list(page_html: str) -> list[dict]:
    """解析旧版列表页，返回帖子行。"""
    out: list[dict] = []
    for lang, body in LIST_ROW.findall(page_html):
        f = lang.split("|")
        if len(f) < 13:
            continue
        href = re.search(r'href="(/bbs/thread/([0-9a-f]{16})/(\d+)-1\.html)"', body)
        title_m = re.search(r'class="a_topic"[^>]*>(.*?)</a>', body, re.S)
        title = clean_text(title_m.group(1)) if title_m else ""
        if not href and not title:
            continue
        out.append({
            "forum_type": f[0],
            "forum_id": int(f[1]) if f[1].isdigit() else None,
            "topic_id": int(f[2]),
            "hash": href.group(2) if href else None,
            "publish_at": norm_date(f[4]),
            "author_id": int(f[5]) if f[5].isdigit() else None,
            "reply_count": int(f[8]) if f[8].isdigit() else 0,
            "author_name": f[10],
            "last_reply_at": norm_date(f[12]),
            "title": title,
        })
    return out


def parse_pages_total(page_html: str) -> int:
    m = re.search(r"共\s*(\d+)\s*页", page_html)
    if m:
        return max(1, min(1000, int(m.group(1))))
    return 1


ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S)
FORUM_HREF = re.compile(r"forum-([a-z])-(\d+)")


def parse_forum_index(page_html: str) -> dict[tuple[str, int], str]:
    """从论坛首页 <a href="...forum-c-18-1.html">奥迪A6L论坛</a> 里抽“论坛目录”。

    首页把 地区 / 车系 / 其他 三类的入口都铺在同一页（1969 个），
    每个链接的锚文本就是论坛名，比逐个论坛再去取标题省 1969 次请求。
    """
    out: dict[tuple[str, int], str] = {}
    for attrs, inner in ANCHOR.findall(page_html):
        m = FORUM_HREF.search(attrs)
        if not m:
            continue
        name = clean_text(inner)
        if not name:
            continue
        key = (m.group(1), int(m.group(2)))
        out.setdefault(key, name)
    return out


def parse_list_total(page_html: str) -> int | None:
    """列表页页脚的「本坛帖子：72016」。"""
    m = re.search(r"本坛帖子[：:]\s*(\d+)", page_html)
    return int(m.group(1)) if m else None


def _balanced_div(s: str, start: int) -> str:
    """从 '<div' 起点截取配对的整段 div。"""
    depth = 0
    pos = start
    pat = re.compile(r"<(/?)div\b", re.I)
    while True:
        m = pat.search(s, pos)
        if not m:
            return s[start:]
        if m.group(1) == "":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                end = s.find(">", m.end())
                return s[start:end + 1]
        pos = m.end()


IMG_ATTR = re.compile(r'(?:src9|data-src|data-original|data-srcset)="([^"]+)"', re.I)
IMG_SRC = re.compile(r'<img[^>]+src="([^"]+)"', re.I)


def extract_images(content_html: str) -> list[str]:
    urls: list[str] = []
    seen = set()
    for raw in IMG_ATTR.findall(content_html) + IMG_SRC.findall(content_html):
        for part in raw.split(","):          # data-srcset 可能是 "url 1x, url 2x"
            url = norm_img(part.strip().split(" ")[0])
            if url and url not in seen and is_real_image(url):
                seen.add(url)
                urls.append(url)
    return urls


def parse_thread_legacy(page_html: str) -> dict:
    """解析旧版帖子页：作者名、日期、正文、src9 图片都在 HTML 里。"""
    title_m = re.search(r'<div class="maxtitle">(.*?)</div>', page_html, re.S)
    if not title_m:
        title_m = re.search(r"<title>(.*?)</title>", page_html, re.S)
    title = clean_text(title_m.group(1)) if title_m else ""
    title = re.sub(r"^【图】", "", title)
    title = re.sub(r"_.*?_汽车之家论坛.*$", "", title).strip()

    forum = re.search(r"/bbs/forum-([aco])-(\d+)-1\.html", page_html)

    # 每个楼层以 xname="uname" 开头，随后是 xname="date" 与 xname="content"
    marks = [m.start() for m in re.finditer(r'xname="uname"', page_html)]
    floors: list[dict] = []
    for i, start in enumerate(marks):
        end = marks[i + 1] if i + 1 < len(marks) else len(page_html)
        block = page_html[start:end]

        uid_m = re.search(r'href="//i\.autohome\.com\.cn/(\d+)/home\.html"', block)
        name_m = re.search(r'xname="uname"[^>]*>(.*?)</a>', block, re.S)
        date_m = re.search(r'xname="date">(.*?)</span>', block, re.S)
        content_start = block.find('xname="content"')
        content = ""
        if content_start >= 0:
            div_start = block.rfind("<div", 0, content_start)
            content = _balanced_div(block, div_start) if div_start >= 0 else block[content_start:]

        floors.append({
            "member_id": int(uid_m.group(1)) if uid_m else None,
            "author": clean_text(name_m.group(1)) if name_m else "",
            "posted_at": norm_date(clean_text(date_m.group(1))) if date_m else "",
            "text": clean_text(content),
            "images": extract_images(content),
        })

    if not floors:
        return {"title": title, "ok": False, "reason": "no-floors", "floors": [], "pages": 1}

    return {
        "title": title or floors[0]["text"][:40],
        "ok": True,
        "floors": floors,
        "pages": parse_pages_total(page_html),
        "forum_type": forum.group(1) if forum else None,
        "forum_id": int(forum.group(2)) if forum else None,
    }


def parse_thread_new(page_html: str) -> dict:
    """新版模板解析：正文在 div.post-container，回复在 li.js-reply-floor-container。

    注意：新版把作者名交给 JS 异步加载，HTML 里只有 data-user-id / data-member-id，
    作者名需要用列表行里的作者名兜底（或显示为“车友{id}”）。
    """
    title_m = re.search(r'<div class="post-title">(.*?)</div>', page_html, re.S)
    title = clean_text(title_m.group(1)) if title_m else ""
    date_m = re.search(r"post-handle-publish.*?<strong[^>]*>(.*?)</strong>", page_html, re.S)
    date = clean_text(date_m.group(1)) if date_m else ""
    mid_m = re.search(r'js-user-info-container"\s+data-user-id="(\d+)"', page_html)
    if not mid_m:
        mid_m = re.search(r'data-user-id="(\d+)"', page_html)

    floors: list[dict] = []
    # 楼主正文
    op_start = page_html.find('<div class="post-container"')
    if op_start >= 0:
        op_html = _balanced_div(page_html, op_start)
        floors.append({
            "member_id": int(mid_m.group(1)) if mid_m else None,
            "author": "",
            "posted_at": date,
            "text": clean_text(op_html),
            "images": extract_images(op_html),
        })

    for m in re.finditer(r'<li[^>]*class="js-reply-floor-container[^"]*"[^>]*>', page_html):
        seg = page_html[m.start(): m.start() + 40000]
        mid = re.search(r'data-member-id="(\d+)"', seg)
        if "reply-detail-deleted" in seg:
            floors.append({"member_id": int(mid.group(1)) if mid else None, "author": "",
                           "posted_at": "", "text": "[该楼层已删除]", "images": []})
            continue
        body = re.search(r'<div class="reply-detail">(.*?)<div class="reply-bottom"', seg, re.S)
        body_html = body.group(1) if body else ""
        dt = re.search(r'reply-static-text[^>]*>\s*<strong[^>]*>(.*?)</strong>', seg, re.S)
        floors.append({
            "member_id": int(mid.group(1)) if mid else None,
            "author": "",
            "posted_at": clean_text(dt.group(1)) if dt else "",
            "text": clean_text(body_html),
            "images": extract_images(body_html),
        })
    return {"title": title, "ok": bool(floors), "floors": floors,
            "pages": parse_pages_total(page_html), "topic_date": date}


# --------------------------------------------------------------------------- #
# 抓取逻辑
# --------------------------------------------------------------------------- #
def legacy_url(hash_: str, topic_id: int, page: int = 1) -> str:
    return f"{CLUB}/o/bbs/thread/{hash_}/{topic_id}-{page}.html"


def canonical_url(hash_: str, topic_id: int, page: int = 1) -> str:
    return f"{CLUB}/bbs/thread/{hash_}/{topic_id}-{page}.html"


def resolve_topic(http: Http, topic_id: int, forum_type: str = "a",
                  forum_id: int = 100004) -> dict:
    """走 301 拿规范 URL（含 hash），并判断帖子是否还在。"""
    probe = f"{CLUB}/bbs/thread-{forum_type}-{forum_id}-{topic_id}-1.html"
    r = http.get(probe, allow_redirects=False)
    loc = r.headers.get("Location", "")
    if r.status_code in (301, 302) and "/bbs/thread/" in loc:
        m = re.search(r"/bbs/thread/([0-9a-f]{16})/(\d+)-1\.html", loc)
        if m:
            return {"ok": True, "hash": m.group(1), "topic_id": int(m.group(2)),
                    "url": loc if loc.startswith("http") else CLUB + loc}
    return {"ok": False, "reason": f"http{r.status_code}"}


def save_gz(path: Path, text: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = text.encode("utf-8", "replace")
    with gzip.open(path, "wb", compresslevel=6) as fh:
        fh.write(raw)
    return len(raw)


def fetch_topic(store: Store, http: Http, row: sqlite3.Row,
                data_dir: Path, max_pages: int = 200) -> dict:
    """抓一个帖子的所有分页。"""
    topic_id = row["topic_id"]
    hash_ = row["hash"]
    if not hash_:
        r = resolve_topic(http, topic_id, row["forum_type"] or "a", row["forum_id"] or 100004)
        if not r["ok"]:
            store.exec("UPDATE topics SET status='deleted', error=?, fetched_at=? WHERE topic_id=?",
                       (r["reason"], now(), topic_id))
            return {"topic_id": topic_id, "status": "deleted"}
        hash_ = r["hash"]
        store.exec("UPDATE topics SET hash=?, url=? WHERE topic_id=?",
                   (hash_, canonical_url(hash_, topic_id), topic_id))

    pages_total = row["pages_total"] or 0
    page = 1
    done = 0
    deleted = False
    while page <= max(1, min(pages_total or 1, max_pages)):
        url = legacy_url(hash_, topic_id, page)
        resp = http.get(url)
        body = resp.text if resp.content else ""
        low = len(body) < 26000

        parsed = parse_thread_legacy(body)
        if not parsed["ok"]:
            # 旧版地址有时也直接返回新版模板，先就地重试，避免多打一次请求
            parsed = parse_thread_new(body)
        if not parsed["ok"]:
            resp2 = http.get(canonical_url(hash_, topic_id, page))
            body2 = resp2.text if resp2.content else ""
            parsed = parse_thread_new(body2)
            if not parsed["ok"]:
                if page == 1:
                    deleted = True
                break
            body = body2

        save_gz(data_dir / "raw" / str(topic_id) / f"{page:03d}.html.gz", body)
        _store_page(store, topic_id, page, parsed)

        if not pages_total:
            pages_total = parsed.get("pages", 1)
            store.exec("UPDATE topics SET pages_total=? WHERE topic_id=?", (pages_total, topic_id))
        done += 1

        if parsed.get("title"):
            store.exec("UPDATE topics SET title=COALESCE(NULLIF(title,''), ?) WHERE topic_id=?",
                       (parsed["title"], topic_id))
        if page == 1 and parsed["floors"]:
            f0 = parsed["floors"][0]
            if f0.get("member_id"):
                store.exec("UPDATE topics SET author_id=COALESCE(author_id, ?), "
                           "author_name=COALESCE(NULLIF(author_name,''), ?) WHERE topic_id=?",
                           (f0["member_id"], f0["author"] or "", topic_id))
            if f0.get("posted_at"):
                store.exec("UPDATE topics SET publish_at=COALESCE(publish_at, ?) WHERE topic_id=?",
                           (f0["posted_at"], topic_id))
            # 只有一楼、正文与图片都为空 → 视为失效帖（图片帖正文可能为空，但通常有图）
            if len(parsed["floors"]) == 1 and not f0.get("text") and not f0.get("images"):
                deleted = True
                break

        page += 1
        if parsed.get("pages"):
            pages_total = min(pages_total or parsed["pages"], parsed["pages"])

    if deleted:
        store.exec("UPDATE topics SET status='deleted', pages_done=?, fetched_at=?, "
                   "image_count=(SELECT COUNT(*) FROM images WHERE topic_id=?) WHERE topic_id=?",
                   (done, now(), topic_id, topic_id))
        return {"topic_id": topic_id, "status": "deleted", "pages": done}

    total_imgs = store.query(
        "SELECT COUNT(*) c FROM images WHERE topic_id=?", (topic_id,))[0]["c"]
    status = "done" if done >= (pages_total or 1) else "partial"
    store.exec("UPDATE topics SET status=?, pages_done=?, pages_total=?, image_count=?, "
               "fetched_at=? WHERE topic_id=?",
               (status, done, pages_total or done, total_imgs, now(), topic_id))
    return {"topic_id": topic_id, "status": status, "pages": done, "images": total_imgs}


def _store_page(store: Store, topic_id: int, page: int, parsed: dict) -> None:
    floors = parsed["floors"]
    for idx, fl in enumerate(floors):
        floor_no = (page - 1) * 20 + idx
        store.exec(
            "INSERT OR REPLACE INTO floors (topic_id,page,floor_no,member_id,author,posted_at,text,images) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (topic_id, page, floor_no, fl.get("member_id"), fl.get("author"),
             norm_date(fl.get("posted_at") or ""), fl.get("text"),
             json.dumps(fl.get("images", []), ensure_ascii=False)),
        )
        for seq, url in enumerate(fl.get("images", [])):
            store.exec(
                "INSERT OR IGNORE INTO images (topic_id,page,floor_no,seq,url,is_face) "
                "VALUES (?,?,?,?,?,?)",
                (topic_id, page, floor_no, seq, url, 1 if is_face_url(url) else 0),
            )


def _wayback_image(http: Http, url: str) -> tuple:
    """原站图片 404 时，去 Wayback 找同一张图（返回 (bytes, timestamp)）。"""
    try:
        r = http.get(WB_CDX, params={"url": url, "output": "json", "limit": "-3",
                                     "fl": "timestamp"}, timeout=60)
        if r.status_code == 429:
            time.sleep(6)
            r = http.get(WB_CDX, params={"url": url, "output": "json", "limit": "-3",
                                         "fl": "timestamp"}, timeout=60)
        if r.status_code == 429:
            return None, "429"
        if r.status_code != 200 or not (r.text or "").strip().startswith("["):
            return None, ""
        rows = json.loads(r.text)
        if len(rows) < 2:
            return None, ""
        ts = rows[-1][0]
        g = http.get(f"https://web.archive.org/web/{ts}im_/{url}", timeout=120)
        ctype = (g.headers.get("Content-Type") or "").lower()
        if g.status_code == 200 and g.content and ctype.startswith("image/"):
            return g.content, ts
    except Exception:  # noqa: BLE001
        pass
    return None, ""


def download_image(http: Http, store: Store, row: sqlite3.Row, data_dir: Path) -> dict:
    url = row["url"]
    if row["is_face"] if "is_face" in row.keys() else is_face_url(url):
        store.exec("UPDATE images SET status='face' WHERE id=?", (row["id"],))
        return {"id": row["id"], "status": "face", "bytes": 0}
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        ext = ".jpg"
    rel = (Path("img") / str(row["topic_id"])
           / f"{(row['floor_no'] or 0):03d}-{(row['seq'] or 0):02d}-{row['id']}{ext}")
    dest = data_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        size = dest.stat().st_size
        store.exec("UPDATE images SET status='done', local_path=?, bytes=?, sha1=? WHERE id=?",
                   (rel.as_posix(), size, hashlib.sha1(dest.read_bytes()).hexdigest(),
                    row["id"]))
        return {"id": row["id"], "status": "done", "bytes": size}

    code, content = 0, None
    try:
        r = http.get(url, headers={"Referer": CLUB + "/"})
        code, content = r.status_code, r.content
    except Exception as exc:  # noqa: BLE001
        note = f"fetch:{str(exc)[:120]}"
    else:
        note = f"http{code}"

    if content and code == 200:
        dest.write_bytes(content)
        store.exec("UPDATE images SET status='done', local_path=?, bytes=?, sha1=? WHERE id=?",
                   (rel.as_posix(), len(content), hashlib.sha1(content).hexdigest(),
                    row["id"]))
        return {"id": row["id"], "status": "done", "bytes": len(content)}

    # 原站没了（2007 前后的 /album/ 图大量 404）→ 去 Wayback 兜底
    if code in (0, 403, 404, 410):
        wb_bytes, ts = _wayback_image(http, url)
        if wb_bytes:
            dest.write_bytes(wb_bytes)
            store.exec("UPDATE images SET status='done', local_path=?, bytes=?, sha1=?, "
                       "error=? WHERE id=?",
                       (rel.as_posix(), len(wb_bytes),
                        hashlib.sha1(wb_bytes).hexdigest(), f"wayback:{ts}", row["id"]))
            return {"id": row["id"], "status": "done", "bytes": len(wb_bytes), "wayback": ts}
        if ts == "429":
            store.exec("UPDATE images SET status='new', error='wb429' WHERE id=?", (row["id"],))
            return {"id": row["id"], "status": "new", "note": "wb429"}

    store.exec("UPDATE images SET status='error', error=? WHERE id=?", (note, row["id"]))
    return {"id": row["id"], "status": "error", "code": code}


# --------------------------------------------------------------------------- #
# Wayback
# --------------------------------------------------------------------------- #
def legacy_topic_url(topic_id: int) -> str:
    """站点早期的 URL 形式（Wayback 里 2006–2015 的快照基本都是这一种）。"""
    return f"https://club.autohome.com.cn/bbs/thread-a-100004-{topic_id}-1.html"


def wb_archived_any(http: Http, urls: list) -> dict:
    """依次查多个 URL 变体，任一有快照即算已归档。"""
    for u in urls:
        info = wb_available(http, u)
        if info.get("available"):
            info["checked"] = u
            return info
    return {"available": False, "checked": urls[0] if urls else ""}


def wb_available(http: Http, url: str) -> dict:
    """CDX 精确查询该 URL 是否有快照。

    注意：不能用 archive.org/wayback/available —— 它按 URL 字符串精确匹配，
    而 CDX 里存的是 `http://club.autohome.com.cn:80/...` 这种带端口的旧形式，
    可用性 API 会因此误报“未归档”。CDX 会做 URL 规范化，判断才准。
    """
    try:
        r = http.get(WB_CDX, params={"url": url, "output": "json", "limit": "2",
                                     "fl": "timestamp,original,statuscode"}, timeout=60)
        text = (r.text or "").strip()
        if r.status_code == 200 and text.startswith("["):
            rows = json.loads(text)
            if len(rows) > 1:
                return {"available": True, "timestamp": rows[1][0],
                        "snapshot": f"https://web.archive.org/web/{rows[1][0]}/{url}"}
            return {"available": False}
    except Exception:  # noqa: BLE001
        pass
    # CDX 失败时退回可用性接口
    r = http.get(WB_AVAILABLE, params={"url": url}, timeout=40)
    snap = (r.json().get("archived_snapshots") or {}).get("closest") or {}
    if snap.get("available"):
        return {"available": True, "timestamp": snap.get("timestamp"), "snapshot": snap.get("url")}
    return {"available": False}


def wb_submit(http: Http, url: str, poll_sec: int = 150) -> dict:
    """把 URL 交给 Wayback Save Page Now。

    SPN 的实际协议：POST /save/ 返回一张 HTML 页面，页面里调用
    spn.watchJob("spn2-xxxx", ...)；随后轮询 /save/status/<job>，
    成功时 JSON 带 14 位 timestamp。
    """
    try:
        r = http.request("POST", WB_SAVE, data={"url": url}, headers={"Referer": WB_SAVE},
                         timeout=90)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"request:{str(exc)[:120]}"}

    final = r.url or ""
    m = re.search(r"/web/(\d{14})/", final)
    if m:
        return {"ok": True, "timestamp": m.group(1), "detail": "direct"}

    body = r.text or ""
    job = re.search(r"watchJob\(\s*['\"](spn2-[0-9a-f]+)", body)
    if not job:
        job = re.search(r"(spn2-[0-9a-f]{20,})", body)
    if not job:
        return {"ok": False, "detail": f"no-job http{r.status_code}"}

    jid = job.group(1)
    deadline = time.time() + poll_sec
    delay = 6.0
    while time.time() < deadline:
        time.sleep(delay)
        delay = min(delay * 1.25, 20.0)
        try:
            s = http.get(f"https://web.archive.org/save/status/{jid}", timeout=60)
            st = s.json()
        except Exception:  # noqa: BLE001
            continue
        status = st.get("status")
        if status in ("success", "success-batch"):
            return {"ok": True, "timestamp": st.get("timestamp", ""),
                    "detail": f"job {jid} http{st.get('http_status')}"}
        if status in ("error", "failed"):
            return {"ok": False, "detail": f"job {jid} {status} {st.get('message', '')}"[:200]}
    return {"ok": False, "detail": f"timeout job {jid}"}


def wb_cdx(http: Http, pattern: str, start: str, end: str, pages: int = 10) -> list[str]:
    urls: list[str] = []
    for page in range(pages):
        params = {
            "url": pattern, "output": "json", "fl": "original",
            "collapse": "urlkey", "limit": "2000", "page": str(page),
            "from": start, "to": end,
        }
        try:
            r = http.get(WB_CDX, params=params, timeout=90)
            if r.status_code != 200 or not r.text.strip():
                break
            rows = r.json()
        except Exception:  # noqa: BLE001
            break
        if not rows:
            break
        added = 0
        for row in rows[1:]:
            u = row[0]
            if u not in urls:
                urls.append(u)
                added += 1
        if added == 0:
            break
    return urls


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def cmd_init(args) -> None:
    store = Store(Path(args.data_dir))
    print(f"数据库: {store.db_path}")
    print("目录: raw/  img/")
    store.close()


def cmd_list(args) -> None:
    store = Store(Path(args.data_dir))
    http = Http(RateLimiter(args.rps))
    lo, hi = parse_range(args.pages)
    added = 0
    for page in range(lo, hi + 1):
        url = f"{CLUB}/o/bbs/forum-{args.forum}-{args.fid}-{page}.html"
        qs = []
        if getattr(args, "refine", False):
            qs.append("type=101")
        if args.order:
            qs.append(f"orderby={args.order}")
        if qs:
            url += "?" + "&".join(qs)
        r = http.get(url)
        if r.status_code != 200:
            print(f"  page {page}: http{r.status_code}")
            continue
        rows = parse_list(r.text)
        for t in rows:
            store.upsert_topic(topic_id=t["topic_id"], forum_type=t["forum_type"],
                               forum_id=t["forum_id"], hash=t["hash"], title=t["title"],
                               author_id=t["author_id"], author_name=t["author_name"],
                               publish_at=t["publish_at"], reply_count=t["reply_count"],
                               last_reply_at=t["last_reply_at"], source="list",
                               url=canonical_url(t["hash"], t["topic_id"]) if t["hash"] else None,
                               status="new")
            added += 1
        print(f"  page {page}: {len(rows)} 帖 (累计 {added})")
        if args.delay:
            time.sleep(args.delay)
        if not rows:
            print("  空页，停止")
            break
    print(f"完成，写入 {added} 行")
    store.close()


def cmd_forums(args) -> None:
    """导入论坛目录（地区 / 车系 / 其他），可选探查各论坛的帖子规模。"""
    store = Store(Path(args.data_dir))
    http = Http(RateLimiter(args.rps))
    found: dict[tuple[str, int], str] = {}

    if args.file:
        html = Path(args.file).read_text(encoding="utf-8", errors="ignore")
        found.update(parse_forum_index(html))
        print(f"从 {args.file} 读到 {len(found)} 个论坛")
    else:
        for url in (f"{CLUB}/bbs/", f"{CLUB}/"):
            r = http.get(url)
            if r.status_code != 200:
                print(f"  {url} -> http{r.status_code}")
                continue
            got = parse_forum_index(r.text)
            print(f"  {url} -> {len(got)} 个论坛")
            found.update(got)

    for (t, i), name in found.items():
        store.upsert_forum(t, i, name)

    # 库里出现过、目录里没有的（例如自己加的 forum/fid）也补一行，名字留空
    for row in store.query("SELECT DISTINCT forum_type, forum_id FROM topics "
                           "WHERE forum_type IS NOT NULL AND forum_id IS NOT NULL"):
        store.upsert_forum(row["forum_type"], int(row["forum_id"]))

    rows = store.query("SELECT bbs_type, COUNT(*) n FROM forums GROUP BY bbs_type")
    print("论坛目录：" + "，".join(f"{r['bbs_type']}={r['n']}" for r in rows)
          + f"，共 {sum(r['n'] for r in rows)} 个")

    # ---- 探查规模 ----
    targets: list[tuple[str, int]] = []
    if args.probe_prov:
        targets += [(r["bbs_type"], int(r["bbs_id"])) for r in store.query(
            "SELECT bbs_type, bbs_id FROM forums WHERE bbs_type='a' AND bbs_id <= ? "
            "ORDER BY bbs_id", (args.prov_max,))]
    if args.probe_ids:
        for spec in args.probe_ids.split(","):
            spec = spec.strip()
            if not spec:
                continue
            t, _, i = spec.partition("-")
            if t and i.isdigit():
                targets.append((t, int(i)))

    if not targets:
        store.close()
        return

    print(f"== 探查 {len(targets)} 个论坛 ==")
    for t, i in targets:
        name = (store.query("SELECT name FROM forums WHERE bbs_type=? AND bbs_id=?",
                            (t, i)) or [{"name": ""}])[0]["name"]
        url = f"{CLUB}/o/bbs/forum-{t}-{i}-1.html"
        try:
            r = http.get(url)
        except Exception as exc:  # noqa: BLE001
            print(f"  {t}-{i} {name}: {type(exc).__name__}")
            continue
        if r.status_code != 200:
            print(f"  {t}-{i} {name}: http{r.status_code}")
            continue
        total = parse_list_total(r.text)
        pages = parse_pages_total(r.text)
        store.upsert_forum(t, i, name or None, topic_total=total)
        if args.keep:
            for row in parse_list(r.text)[:args.keep]:
                store.upsert_topic(topic_id=row["topic_id"], forum_type=row["forum_type"],
                                   forum_id=row["forum_id"], hash=row["hash"],
                                   title=row["title"], author_id=row["author_id"],
                                   author_name=row["author_name"],
                                   publish_at=row["publish_at"],
                                   reply_count=row["reply_count"],
                                   last_reply_at=row["last_reply_at"],
                                   source="forum-probe",
                                   url=canonical_url(row["hash"], row["topic_id"])
                                   if row["hash"] else None,
                                   status="new")
        newest = parse_list(r.text)
        span = ""
        if newest:
            span = f" 最新 {newest[0]['publish_at'][:10]}"
        print(f"  {t}-{i} {name}: 本坛帖子 {total}，{pages} 页{span}")

    store.close()


def cmd_cdx(args) -> None:
    store = Store(Path(args.data_dir))
    http = Http(RateLimiter(args.rps), timeout=90)
    pattern = f"club.autohome.com.cn/bbs/thread-{args.forum}-{args.fid}-*"
    urls = wb_cdx(http, pattern, args.start, args.end, args.cdx_pages)
    print(f"CDX 返回 {len(urls)} 个 URL")
    added = 0
    for u in urls:
        m = re.search(r"/bbs/thread-(?:[aco])-\d+-(\d+)-\d+\.html", u)
        if not m:
            continue
        tid = int(m.group(1))
        exists = store.query("SELECT 1 FROM topics WHERE topic_id=?", (tid,))
        if exists:
            continue          # 已有记录（可能已抓完/已删）不动它的 status
        store.upsert_topic(topic_id=tid, forum_type=args.forum, forum_id=args.fid,
                           source="cdx", status="new")
        added += 1
    print(f"新增早期帖子 {added} 条（其余已存在）")
    store.close()


def cmd_resolve(args) -> None:
    store = Store(Path(args.data_dir))
    http = Http(RateLimiter(args.rps))
    rows = store.query("SELECT * FROM topics WHERE hash IS NULL ORDER BY topic_id LIMIT ?", (args.limit,))
    ok = bad = 0
    for row in rows:
        r = resolve_topic(http, row["topic_id"], row["forum_type"] or args.forum, row["forum_id"] or args.fid)
        if r["ok"]:
            store.exec("UPDATE topics SET hash=?, url=?, status='new' WHERE topic_id=?",
                       (r["hash"], r["url"], row["topic_id"]))
            ok += 1
        else:
            store.exec("UPDATE topics SET status='deleted', error=? WHERE topic_id=?",
                       (r["reason"], row["topic_id"]))
            bad += 1
        print(f"  {row['topic_id']}: {'ok' if r['ok'] else r['reason']}")
    print(f"解析完成 ok={ok} 失效={bad}")
    store.close()


def cmd_fetch(args) -> None:
    store = Store(Path(args.data_dir))
    http = Http(RateLimiter(args.rps))
    args.limit = max(1, min(int(args.limit), MAX_BATCH * 2))
    if getattr(args, "valid", 0):
        args.valid = max(1, min(int(args.valid), MAX_BATCH))
    if args.topic:
        for tid in args.topic:
            if not store.query("SELECT 1 FROM topics WHERE topic_id=?", (tid,)):
                store.upsert_topic(topic_id=tid, forum_type=args.forum, forum_id=args.fid,
                                   source="seed", status="new")
        rows = store.query(
            "SELECT * FROM topics WHERE topic_id IN (%s)" % ",".join("?" * len(args.topic)),
            args.topic)
        return _fetch_rows(store, http, args, rows, target=0)

    order = getattr(args, "order", "default")
    if order == "id-asc":
        clause = "ORDER BY topic_id ASC"
    elif order == "id-desc":
        clause = "ORDER BY topic_id DESC"
    elif order == "oldest":
        clause = "ORDER BY publish_at IS NULL, publish_at ASC, topic_id ASC"
    elif order == "newest":
        clause = "ORDER BY publish_at IS NULL, publish_at DESC, topic_id DESC"
    else:
        clause = ("ORDER BY (source='cdx') DESC, publish_at IS NULL DESC, "
                  "publish_at, topic_id")

    target = max(0, getattr(args, "valid", 0) or 0)
    # 可指定游标起点（--from-id / --to-id），用于跳过已经遍历过的号段
    lo = getattr(args, "from_id", 0) or 0
    hi = getattr(args, "to_id", 0) or 0

    only_forum = (getattr(args, "only_forum", None) or "").strip()

    def _scope() -> tuple[str, tuple]:
        bits, vals = [], []
        if lo:
            bits.append("topic_id >= ?")
            vals.append(int(lo))
        if hi:
            bits.append("topic_id <= ?")
            vals.append(int(hi))
        if only_forum and "-" in only_forum:
            ftype, _, fid = only_forum.partition("-")
            if fid.isdigit():
                bits.append("forum_type = ? AND forum_id = ?")
                vals += [ftype, int(fid)]
        return (" AND " + " AND ".join(bits)) if bits else "", tuple(vals)

    extra, extra_vals = _scope()
    if lo or hi:
        print(f"游标范围：{'id>=%d' % lo if lo else ''}"
              f"{' ' if (lo and hi) else ''}{'id<=%d' % hi if hi else ''}")
    if not target:
        rows = store.query(
            "SELECT * FROM topics WHERE status IN ('new','partial','error')"
            + extra + " " + clause + " LIMIT ?", extra_vals + (args.limit,))
        return _fetch_rows(store, http, args, rows, target=0)

    # --valid N：一直抓到 N 篇“真的抓到内容”的帖子为止（已删/空贴不算数）
    max_attempts = getattr(args, "max_attempts", 0) or target * 6
    got = tried = 0
    while got < target and tried < max_attempts:
        want = min(args.limit, max(1, (target - got) * 2))
        rows = store.query(
            "SELECT * FROM topics WHERE status IN ('new','partial','error')"
            + extra + " " + clause + " LIMIT ?", extra_vals + (want,))
        if not rows:
            print("队列已空")
            break
        got += _fetch_rows(store, http, args, rows, target=target - got, quiet_tail=True)
        tried += len(rows)
        print(f"[有效帖 {got}/{target}，已尝试 {tried} 帖]")
    print(f"抓取完成：有效 {got} 篇 / 尝试 {tried} 帖")
    store.close()
    return got


def _fetch_rows(store, http, args, rows, target=0, quiet_tail=False) -> int:
    """抓一批帖子，返回其中“真的抓到内容”的数量。"""
    if not rows:
        print("没有待抓取的帖子")
        return 0
    wanted = min(target, len(rows)) if target else 0
    print(f"待抓取 {len(rows)} 帖，并发 {args.workers}"
          + (f"，目标有效 {wanted} 帖" if target else ""))
    done = valid = 0
    blocks = 0
    stopped = False
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(fetch_topic, store, http, row, Path(args.data_dir), args.max_pages): row
                for row in rows}
        for fut in as_completed(futs):
            row = futs[fut]
            try:
                res = fut.result()
            except Blocked as exc:
                blocks += 1
                print(f"  !! 风控拦截（{blocks}）：{exc}；这些帖保持待抓，不会被误判成已删")
                if blocks >= 3:
                    stopped = True
                    for f in futs:
                        f.cancel()
                    break
                continue
            except Exception as exc:  # noqa: BLE001
                store.exec("UPDATE topics SET status='error', error=?, fetched_at=? WHERE topic_id=?",
                           (str(exc)[:300], now(), row["topic_id"]))
                print(f"  {row['topic_id']}: ERROR {str(exc)[:100]}")
                continue
            done += 1
            if res["status"] == "done":
                valid += 1
            print(f"  {row['topic_id']}: {res['status']} pages={res.get('pages', 0)} "
                  f"imgs={res.get('images', 0)} "
                  f"[{done}/{len(rows)}" + (f"，有效 {valid}" if target else "") + "]")
    if stopped:
        print("本轮因风控中止：请降低 --rps / --workers，或过几十分钟再跑；"
              "已抓到的内容都已入库，未抓的仍是待抓状态")
    if not quiet_tail:
        print("抓取完成")
    return valid


def cmd_images(args) -> None:
    store = Store(Path(args.data_dir))
    http = Http(RateLimiter(args.rps))
    args.limit = max(1, min(int(args.limit), MAX_BATCH))
    where = ["COALESCE(is_face,0)=0"]
    if getattr(args, "topic", None):
        where.append(f"topic_id={int(args.topic)}")
    if getattr(args, "retry_errors", False):
        where.append("status IN ('new','error')")
    else:
        where.append("status='new'")
    rows = store.query("SELECT * FROM images WHERE " + " AND ".join(where)
                       + " ORDER BY id LIMIT ?", (args.limit,))
    print(f"待下载图片 {len(rows)}")
    ok = err = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(download_image, http, store, row, Path(args.data_dir)) for row in rows]
        for i, fut in enumerate(as_completed(futs), 1):
            res = fut.result()
            if res["status"] == "done":
                ok += 1
            else:
                err += 1
            if i % 50 == 0 or i == len(futs):
                print(f"  {i}/{len(futs)} ok={ok} err={err}")
    print("图片下载完成")
    store.close()


def cmd_wayback(args) -> None:
    store = Store(Path(args.data_dir))
    http = Http(RateLimiter(args.rps), timeout=60)
    if args.topic:
        rows = store.query("SELECT * FROM topics WHERE topic_id=?", (args.topic,))
    else:
        rows = store.query(
            "SELECT * FROM topics WHERE hash IS NOT NULL AND topic_id NOT IN "
            "(SELECT topic_id FROM wayback WHERE topic_id IS NOT NULL AND status IN ('exists','submitted')) "
            "ORDER BY publish_at LIMIT ?", (args.limit,))
    print(f"待检查 {len(rows)} 帖 (submit={args.submit})")
    for row in rows:
        url = canonical_url(row["hash"], row["topic_id"])
        legacy = legacy_topic_url(row["topic_id"])
        av = wb_archived_any(http, [url, legacy])
        if av["available"]:
            store.exec("INSERT OR REPLACE INTO wayback (url,topic_id,checked_at,available,snapshot_ts,status,note) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (url, row["topic_id"], now(), 1, av.get("timestamp"), "exists",
                        av.get("checked", "")))
            print(f"  {row['topic_id']}: 已归档 {av.get('timestamp')}")
            continue
        if not args.submit or args.dry_run:
            store.exec("INSERT OR REPLACE INTO wayback (url,topic_id,checked_at,available,status,note) "
                       "VALUES (?,?,?,?,?,?)",
                       (url, row["topic_id"], now(), 0, "skipped", "dry-run"))
            print(f"  {row['topic_id']}: 未归档（仅记录，未提交）")
            continue
        res = wb_submit(http, url)
        store.exec("INSERT OR REPLACE INTO wayback (url,topic_id,checked_at,available,submitted_at,status,note) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (url, row["topic_id"], now(), 0, now(),
                    "submitted" if res.get("ok") else "failed", str(res.get("detail", ""))[:300]))
        print(f"  {row['topic_id']}: 提交 -> {res.get('ok')} {res.get('detail', '')}")
        time.sleep(args.submit_interval)
    store.close()


def cmd_recheck(args) -> None:
    """重新校验被判“已删”的帖子：风控期间这类判定可能全是假的。"""
    store = Store(Path(args.data_dir))
    http = Http(RateLimiter(args.rps))
    where = ["status='deleted'"]
    params: list = []
    if getattr(args, "since", None):
        where.append("fetched_at >= ?")
        params.append(args.since)
    rows = store.query("SELECT * FROM topics WHERE " + " AND ".join(where)
                       + " ORDER BY topic_id LIMIT ?", params + [args.limit])
    print(f"待复核 {len(rows)} 个“已删”判定（rps={args.rps}）")
    back = keep = 0
    for i, row in enumerate(rows, 1):
        try:
            r = resolve_topic(http, row["topic_id"], row["forum_type"] or "a",
                              row["forum_id"] or 100004)
        except Blocked as exc:
            print(f"  !! 又遇到人机验证，停止复核：{exc}")
            break
        if r["ok"]:
            store.exec("UPDATE topics SET status='new', hash=?, url=?, error=NULL, "
                       "fetched_at=? WHERE topic_id=?",
                       (r["hash"], canonical_url(r["hash"], row["topic_id"]), now(),
                        row["topic_id"]))
            back += 1
            print(f"  {row['topic_id']}: 还活着 → 重新入队")
        else:
            store.exec("UPDATE topics SET fetched_at=? WHERE topic_id=?",
                       (now(), row["topic_id"]))
            keep += 1
        if i % 50 == 0:
            print(f"  ...{i}/{len(rows)}  恢复 {back} 维持 {keep}")
    print(f"复核完成：恢复 {back} 帖，维持已删 {keep}")


def cmd_probe(args) -> None:
    """探一下现在能不能抓：站点风控（人机验证）是否已解除。"""
    limiter = RateLimiter(max(0.1, args.rps))
    http = Http(limiter, retries=1, max_blocks=0)
    targets = [
        ("帖子页", f"{CLUB}/bbs/thread/3d6e6942707597f9/13510439-1.html"),
        ("旧版列表", f"{CLUB}/o/bbs/forum-a-100004-1.html"),
        ("新帖 302", f"{CLUB}/bbs/thread-a-100004-116461510-1.html"),
    ]
    blocked = 0
    for name, url in targets:
        try:
            r = http.get(url, timeout=20)
            low = (getattr(r, "url", "") or "").lower()
            if "userverify" in low or "safety.autohome" in low:
                blocked += 1
                print(f"  {name:<8} 人机验证（被拦截）")
            else:
                print(f"  {name:<8} HTTP {r.status_code}  {len(r.content):>7} 字节  正常")
        except Blocked:
            blocked += 1
            print(f"  {name:<8} 人机验证（被拦截）")
        except Exception as exc:  # noqa: BLE001
            print(f"  {name:<8} 出错：{exc}")
    print("结论：" + ("仍在风控中，过 20-60 分钟再试" if blocked else "通道正常，可以继续抓"))


def cmd_status(args) -> None:
    store = Store(Path(args.data_dir))
    rows = store.query("SELECT status, COUNT(*) c FROM topics GROUP BY status ORDER BY c DESC")
    print("== 帖子状态 ==")
    for r in rows:
        print(f"  {r['status']:<10} {r['c']}")
    total = store.query("SELECT COUNT(*) c FROM topics")[0]["c"]
    print(f"  合计       {total}")
    fl = store.query("SELECT COUNT(*) c FROM floors")[0]["c"]
    im = store.query("SELECT COUNT(*) c FROM images WHERE COALESCE(is_face,0)=0")[0]["c"]
    imd = store.query("SELECT COUNT(*) c FROM images WHERE status='done' "
                      "AND COALESCE(is_face,0)=0")[0]["c"]
    uniq = store.query("SELECT COUNT(*) c FROM (SELECT DISTINCT topic_id, "
                       "COALESCE(sha1,url) FROM images WHERE COALESCE(is_face,0)=0)")[0]["c"]
    fc = store.query("SELECT COUNT(*) c FROM images WHERE COALESCE(is_face,0)=1")[0]["c"]
    wb = store.query("SELECT status, COUNT(*) c FROM wayback GROUP BY status")
    rng = store.query("SELECT MIN(publish_at) a, MAX(publish_at) b FROM topics WHERE publish_at IS NOT NULL")
    print(f"== 内容 ==\n  楼层 {fl}  图片 {im}（去重后 {uniq}，已存 {imd}）  默认表情 {fc}（已忽略）")
    pend = store.query("SELECT MIN(topic_id) a, MAX(topic_id) b, COUNT(*) c FROM topics "
                       "WHERE status IN ('new','partial','error')")[0]
    if pend["c"]:
        print(f"== 待抓队列 ==\n  {pend['c']} 帖，ID {pend['a']} ~ {pend['b']}"
              f"（想从这里继续：fetch --from-id {pend['a']}）")
    print(f"== 时间范围 ==\n  {rng[0]['a']} ~ {rng[0]['b']}")
    print("== Wayback ==")
    for r in wb:
        print(f"  {r['status']:<10} {r['c']}")
    size = sum(p.stat().st_size for p in Path(args.data_dir).rglob("*") if p.is_file())
    print(f"== 占用 ==\n  {size / 1024 / 1024:.1f} MB")
    store.close()


def parse_range(spec: str) -> tuple[int, int]:
    if "-" in spec:
        a, b = spec.split("-", 1)
        return int(a), int(b)
    n = int(spec)
    return 1, n


def cmd_export(args) -> None:
    """把一批帖子打成 zip（离线相册 + 楼层文本 + 图片 + 可选原始 HTML）。"""
    import exportlib

    store = Store(Path(args.data_dir))
    ids = args.topic or None
    topics = exportlib.query_topics(store.conn, ids=ids, date_from=args.date_from,
                                    date_to=args.date_to, limit=args.limit,
                                    order=args.order, only_done=args.only_done,
                                    forum=getattr(args, "forum_sel", None))
    if not topics:
        print("没有匹配的帖子（检查时间范围 / 是否已抓取）")
        store.close()
        return
    out = Path(args.out) if args.out else Path(args.data_dir).parent / "exports" / \
        exportlib.default_name(args.date_from, args.date_to, ids)
    stats = exportlib.build_zip(store.conn, Path(args.data_dir), out, topics,
                                include_raw=args.raw)
    store.close()
    print(f"导出 {stats['topics']} 帖 / {stats['floors']} 楼 / {stats['images']} 图"
          f"（缺失 {stats['missing']}，原始 HTML {stats['raw']} 页）")
    print(f"-> {out}  {out.stat().st_size / 1048576:.1f} MB")


def cmd_walk(args) -> None:
    """按 topicId 递增暴力枚举（用于列表够不到的早期区间）。

    每条只需一次 301 解析：存在 -> 记 hash 入队；已删 -> 记入 walked 表不再重试。
    """
    store = Store(Path(args.data_dir))
    http = Http(RateLimiter(args.rps))
    done = store.query("SELECT COUNT(1) c FROM walked")[0]["c"]
    found = skipped = n = 0
    t0 = time.time()
    print(f"遍历 topicId {args.from_id} .. {args.to_id}（已走过 {done} 个）rps={args.rps}")
    for tid in range(args.from_id, args.to_id + 1, args.step):
        if args.max_requests and n >= args.max_requests:
            print("  达到 --max-requests 上限，停止")
            break
        if store.query("SELECT 1 FROM walked WHERE id=?", (tid,)):
            continue
        n += 1
        try:
            r = resolve_topic(http, tid, args.forum, args.fid)
        except Blocked as exc:
            print(f"  !! 遇到人机验证，停止遍历：{exc}")
            print("     已走过的 id 都记在 walked 表里，过一段时间再跑会从断点继续")
            break
        store.exec("INSERT OR REPLACE INTO walked (id,status,checked_at) VALUES (?,?,?)",
                   (tid, "ok" if r["ok"] else r["reason"], now()))
        if r["ok"]:
            store.upsert_topic(topic_id=tid, forum_type=args.forum, forum_id=args.fid,
                               hash=r["hash"], url=r["url"], source="walk", status="new")
            found += 1
        else:
            skipped += 1
        if n % 100 == 0:
            rate = n / max(time.time() - t0, 1e-6)
            eta = (args.to_id - tid) / max(rate, 1e-6) / 3600
            print(f"  {tid}: 命中 {found} 失效 {skipped}  {rate:.1f} id/s  ETA {eta:.1f}h")
    print(f"完成：新增 {found} 帖，失效 {skipped}，共探测 {n}")


def cmd_crawl(args) -> None:
    """从最早的内容开始爬：CDX 播种 -> 解析 -> 按 topicId 升序抓取若干轮。"""
    print("== 1/3 Wayback CDX 播种 ==")
    cmd_cdx(argparse.Namespace(data_dir=args.data_dir, rps=0.2, forum=args.forum,
                               fid=args.fid, start=str(args.from_year),
                               end=str(args.to_year), cdx_pages=args.cdx_pages))
    print("== 2/3 解析缺失的 hash ==")
    cmd_resolve(argparse.Namespace(data_dir=args.data_dir, rps=args.rps, forum=args.forum,
                                   fid=args.fid, limit=args.resolve_limit))
    print("== 3/3 按 topicId 升序抓取（越早的 ID 越早抓）==")
    for rnd in range(1, args.rounds + 1):
        print(f"-- 第 {rnd} 轮 --")
        processed = cmd_fetch(argparse.Namespace(
            data_dir=args.data_dir, rps=args.rps, limit=args.batch,
            topic=None, forum=args.forum, fid=args.fid,
            workers=args.workers, max_pages=args.max_pages,
            order="id-asc")) or 0
        print(f"  本轮处理 {processed} 帖，剩余待抓 {store_count(args.data_dir)}")
        if processed == 0:
            print("  队列已空，收工")
            break


def store_count(data_dir) -> int:
    import sqlite3
    conn = sqlite3.connect(Path(data_dir) / "autohome.db")
    try:
        return conn.execute("SELECT COUNT(1) FROM topics WHERE status IN ('new','partial','error')"
                            ).fetchone()[0]
    finally:
        conn.close()


def cmd_update(args) -> None:
    """一条命令跑一轮增量备份：列表 -> 帖子 -> 图片 -> Wayback 补档。"""
    print("== 1/4 列表 ==")
    cmd_list(argparse.Namespace(data_dir=args.data_dir, rps=args.rps, forum=args.forum,
                                fid=args.fid, pages=f"1-{args.pages}", order=None, delay=0.0))
    print("== 2/4 帖子 ==")
    cmd_fetch(argparse.Namespace(data_dir=args.data_dir, rps=args.rps, limit=args.fetch,
                                 topic=None, forum=args.forum, fid=args.fid,
                                 workers=args.workers, max_pages=args.max_pages))
    print("== 3/4 图片 ==")
    cmd_images(argparse.Namespace(data_dir=args.data_dir, rps=args.rps,
                                  limit=args.images, workers=args.workers))
    print("== 4/4 Wayback ==")
    cmd_wayback(argparse.Namespace(data_dir=args.data_dir, rps=1.0, limit=args.wayback,
                                   topic=None, submit=not args.no_submit, dry_run=False,
                                   submit_interval=args.submit_interval))
    cmd_status(argparse.Namespace(data_dir=args.data_dir))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="汽车之家论坛备份工具")
    p.add_argument("--data-dir", default=str(DEFAULT_DATA))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")

    pl = sub.add_parser("list")

    pl.add_argument("--refine", action="store_true",

                    help="只看精华：加 ?type=101（大论坛能往回收好几年）")
    pl.add_argument("--forum", default="a")
    pl.add_argument("--fid", type=int, default=100004)
    pl.add_argument("--pages", default="1-5")
    pl.add_argument("--order", default=None, help="lastpost | dateline")
    pl.add_argument("--rps", type=float, default=3.0)
    pl.add_argument("--delay", type=float, default=0.0)

    pf = sub.add_parser("forums", help="导入论坛目录（地区/车系/其他），可选探查规模")
    pf.add_argument("--file", default=None, help="从本地保存的首页 HTML 解析，不联网")
    pf.add_argument("--probe-prov", action="store_true", help="探查所有省级地区论坛")
    pf.add_argument("--prov-max", type=int, default=100034, help="省级论坛的 id 上限")
    pf.add_argument("--probe-ids", default=None, help="额外探查，如 c-18,c-66,o-200042")
    pf.add_argument("--keep", type=int, default=0, help="每个被探查论坛顺带入库多少条帖子")
    pf.add_argument("--rps", type=float, default=2.0)

    pc = sub.add_parser("cdx")
    pc.add_argument("--forum", default="a")
    pc.add_argument("--fid", type=int, default=100004)
    pc.add_argument("--start", default="2005")
    pc.add_argument("--end", default="2014")
    pc.add_argument("--cdx-pages", type=int, default=5)
    pc.add_argument("--rps", type=float, default=0.2)

    pr = sub.add_parser("resolve")
    pr.add_argument("--forum", default="a")
    pr.add_argument("--fid", type=int, default=100004)
    pr.add_argument("--limit", type=int, default=200)
    pr.add_argument("--rps", type=float, default=2.0)

    pf = sub.add_parser("fetch")
    pf.add_argument("--limit", type=int, default=50)
    pf.add_argument("--topic", type=int, nargs="+", default=None, help="指定 topicId（可多个）")
    pf.add_argument("--forum", default="a")
    pf.add_argument("--fid", type=int, default=100004)
    pf.add_argument("--workers", type=int, default=2)
    pf.add_argument("--max-pages", type=int, default=50)

    pf.add_argument("--order", default="default",
                    choices=["default", "id-asc", "id-desc", "oldest", "newest"],
                    help="抓取顺序：default 早期优先 | id-asc 按帖子 ID 升序")
    pf.add_argument("--valid", type=int, default=0,
                    help="抓到 N 篇“有内容”的帖子就停（已删/空贴不算数）")
    pf.add_argument("--max-attempts", type=int, default=0,
                    help="配合 --valid：最多尝试多少帖（默认 N*6）")
    pf.add_argument("--from-id", type=int, default=0,
                    help="游标起点：只抓 topicId >= 这个值的帖（跳过已遍历的号段）")
    pf.add_argument("--to-id", type=int, default=0,
                    help="游标终点：只抓 topicId <= 这个值的帖")
    pf.add_argument("--only-forum", dest="only_forum", default=None,
                   help="只抓某个论坛的队列，如 a-100004 / c-18")

    pf.add_argument("--rps", type=float, default=3.0)

    pi = sub.add_parser("images")
    pi.add_argument("--limit", type=int, default=200)
    pi.add_argument("--topic", type=int, default=None, help="只处理某个帖子")
    pi.add_argument("--retry-errors", action="store_true", help="把之前失败的图也重试一遍")
    pi.add_argument("--workers", type=int, default=4)
    pi.add_argument("--rps", type=float, default=4.0)

    pw = sub.add_parser("wayback")
    pw.add_argument("--limit", type=int, default=20)
    pw.add_argument("--topic", type=int, default=None)
    pw.add_argument("--submit", action="store_true", help="未归档时提交 Save Page Now")
    pw.add_argument("--dry-run", action="store_true", help="只检查不提交")
    pw.add_argument("--submit-interval", type=float, default=8.0)
    pw.add_argument("--rps", type=float, default=1.0)

    pe = sub.add_parser("export", help="把一批帖子导出成 zip（离线相册 + 文本 + 图片）")
    pe.add_argument("--out", default=None, help="输出 zip 路径")
    pe.add_argument("--from", dest="date_from", default=None, help="起始年月/日期 2006-01 或 2006-01-01")
    pe.add_argument("--to", dest="date_to", default=None)
    pe.add_argument("--topic", type=int, nargs="+", default=None, help="指定 topicId")
    pe.add_argument("--limit", type=int, default=800)
    pe.add_argument("--order", choices=["asc", "desc"], default="asc")
    pe.add_argument("--only-done", action="store_true", help="只要已抓取完成的帖子")
    pe.add_argument("--raw", action="store_true", help="附带原始 HTML")
    pe.add_argument("--forum", dest="forum_sel", default=None,
                    help="只导出某个论坛，如 a-100004 / c-18")

    pk = sub.add_parser("walk", help="按 topicId 暴力枚举早期区间")
    pk.add_argument("--forum", default="a")
    pk.add_argument("--fid", type=int, default=100004)
    pk.add_argument("--from-id", type=int, required=True)
    pk.add_argument("--to-id", type=int, required=True)
    pk.add_argument("--step", type=int, default=1)
    pk.add_argument("--max-requests", type=int, default=0, help="0 表示不限制")
    pk.add_argument("--rps", type=float, default=2.0)

    pc2 = sub.add_parser("crawl", help="从最早期开始爬（CDX 播种 + 升序抓取）")
    pc2.add_argument("--forum", default="a")
    pc2.add_argument("--fid", type=int, default=100004)
    pc2.add_argument("--from-year", type=int, default=2005)
    pc2.add_argument("--to-year", type=int, default=2013)
    pc2.add_argument("--cdx-pages", type=int, default=5)
    pc2.add_argument("--resolve-limit", type=int, default=300)
    pc2.add_argument("--rounds", type=int, default=3)
    pc2.add_argument("--batch", type=int, default=60)
    pc2.add_argument("--workers", type=int, default=2)
    pc2.add_argument("--max-pages", type=int, default=50)
    pc2.add_argument("--rps", type=float, default=3.0)

    pu = sub.add_parser("update", help="增量备份一轮：列表-帖子-图片-Wayback")
    pu.add_argument("--forum", default="a")
    pu.add_argument("--fid", type=int, default=100004)
    pu.add_argument("--pages", type=int, default=2, help="抓列表前 N 页")
    pu.add_argument("--fetch", type=int, default=50, help="本轮抓多少个帖子")
    pu.add_argument("--images", type=int, default=300)
    pu.add_argument("--wayback", type=int, default=10)
    pu.add_argument("--workers", type=int, default=4)
    pu.add_argument("--max-pages", type=int, default=50)
    pu.add_argument("--submit-interval", type=float, default=8.0)
    pu.add_argument("--no-submit", action="store_true", help="只查不提交 Wayback")
    pu.add_argument("--rps", type=float, default=3.0)

    sub.add_parser("status")
    pp = sub.add_parser("probe", help="检查是否被站点风控拦截（人机验证）")
    pp.add_argument("--rps", type=float, default=0.5)

    prc = sub.add_parser("recheck", help="复核被判“已删”的帖子（风控误判时用）")
    prc.add_argument("--limit", type=int, default=500)
    prc.add_argument("--since", default=None, help="只复核某个时间之后判的，如 '2026-10-27 16:00'")
    prc.add_argument("--rps", type=float, default=2.0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "init": cmd_init, "list": cmd_list, "cdx": cmd_cdx, "resolve": cmd_resolve,
        "fetch": cmd_fetch, "images": cmd_images, "wayback": cmd_wayback, "status": cmd_status,
        "update": cmd_update,
        "export": cmd_export, "walk": cmd_walk, "crawl": cmd_crawl,
        "probe": cmd_probe, "recheck": cmd_recheck, "forums": cmd_forums,
    }
    handlers[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
