"""把有规模的论坛逐个到 Wayback 里播种 2005–2013 的老帖。

用法：python seed_all.py [--min-total 10000] [--cdx-pages 2]
可以反复跑：已经入库的帖子会被跳过。
"""

import argparse
import datetime
import pathlib
import sqlite3
import subprocess
import sys
import time

BASE = pathlib.Path(__file__).resolve().parent
DATA = BASE / "data" / "autohome.db"
LOG = BASE / "seed_all.log"


def log(msg: str) -> None:
    line = f"[{datetime.datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-total", type=int, default=10000)
    ap.add_argument("--cdx-pages", type=int, default=2)
    ap.add_argument("--rps", default="1")
    args = ap.parse_args()

    c = sqlite3.connect(DATA)
    try:
        rows = c.execute(
            "SELECT bbs_type, bbs_id, name, topic_total FROM forums "
            "WHERE topic_total IS NOT NULL AND topic_total >= ? "
            "ORDER BY topic_total DESC", (args.min_total,)).fetchall()
    finally:
        c.close()

    log(f"准备播种 {len(rows)} 个论坛（>= {args.min_total} 帖）")
    total_new = 0
    for i, (ftype, fid, name, n) in enumerate(rows, 1):
        log(f"[{i}/{len(rows)}] {ftype}-{fid} {name}（{n:,} 帖）")
        cmd = [sys.executable, "backup.py", "cdx", "--forum", ftype, "--fid", str(fid),
               "--start", "2005", "--end", "2013",
               "--cdx-pages", str(args.cdx_pages), "--rps", args.rps]
        try:
            r = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=900)
            out = (r.stdout or "").strip().splitlines()
            tail = out[-2:] if len(out) >= 2 else out
            for line in tail:
                log("    " + line.strip())
            for line in out:
                if "新增早期帖子" in line:
                    try:
                        total_new += int(line.split("新增早期帖子")[1].split("条")[0].strip())
                    except ValueError:
                        pass
        except subprocess.TimeoutExpired:
            log("    超时（CDX 太慢），跳过")
        except Exception as e:  # noqa: BLE001
            log(f"    出错：{type(e).__name__} {e}")
        time.sleep(2)

    log(f"播种结束，累计新增 {total_new} 条早期帖子。下一步：python backup.py resolve --limit 20000 --rps 2")


if __name__ == "__main__":
    main()
