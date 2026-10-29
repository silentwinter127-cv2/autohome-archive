"""把「已播种但没解析 / 解析了没抓」的队列慢慢排干。

顺序执行（不并发），一次一小步，避免触发站点的人机验证：
  1) resolve 一小批缺失 hash 的种子
  2) fetch 一小批（按 topicId 升序 = 越早的帖子越先抓）
反复直到队列空。可以随时 Ctrl+C，进度都在库里。
"""

import pathlib
import sqlite3
import subprocess
import sys
import time

BASE = pathlib.Path(__file__).resolve().parent
DATA = BASE / "data" / "autohome.db"


def counts():
    c = sqlite3.connect(DATA)
    try:
        pending = c.execute("SELECT COUNT(*) FROM topics WHERE status IN ('new','partial','error')").fetchone()[0]
        unresolved = c.execute("SELECT COUNT(*) FROM topics WHERE hash IS NULL AND status='new'").fetchone()[0]
        done = c.execute("SELECT COUNT(*) FROM topics WHERE status='done'").fetchone()[0]
    finally:
        c.close()
    return pending, unresolved, done


def run(args, timeout=3600):
    r = subprocess.run([sys.executable, "backup.py"] + args, cwd=BASE,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    tail = [ln for ln in (r.stdout or "").strip().splitlines() if ln.strip()][-2:]
    for ln in tail:
        print("   " + ln, flush=True)


def main():
    rounds = 0
    while True:
        rounds += 1
        pending, unresolved, done = counts()
        print(f"\n=== 第 {rounds} 轮 | 待抓 {pending} / 待解析 {unresolved} / 已完成 {done} ===",
              flush=True)
        if pending == 0 and unresolved == 0:
            print("队列已空，收工。", flush=True)
            break
        if unresolved:
            run(["resolve", "--limit", "400", "--rps", "1.5"])
        run(["fetch", "--order", "id-asc", "--valid", "80", "--limit", "200",
             "--max-attempts", "400", "--workers", "2", "--rps", "1.8"])
        time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("已停止（进度都在库里）")
