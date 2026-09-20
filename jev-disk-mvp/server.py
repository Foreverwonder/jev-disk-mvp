#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JEV 硬盘体检 · MVP 服务端

用法:
    set TYPESAFE_API_KEY=apikey_xxx
    python server.py            # 默认 http://127.0.0.1:8848

全程只读：不删除、不移动、不改名任何文件。
所有判据与权重来自 config.json，代码里不写死。
"""
import argparse
import concurrent.futures
import heapq
import json
import re
import math
import os
import string
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
CACHE_DIR = os.path.join(HERE, "cache")
API = "https://api.typesafe.ai/v1/systemone"

CFG = json.load(open(CONFIG_PATH, encoding="utf-8"))
MODEL = CFG["model"]
API_KEY = os.environ.get("TYPESAFE_API_KEY", "")
JOBS = {}
JOBS_LOCK = threading.Lock()
HOME = os.path.expanduser("~")
LAST_HIT = [time.time()]          # 最后一次收到请求的时刻
SHUTDOWN = threading.Event()
SRV = CFG["server"]


def touch():
    LAST_HIT[0] = time.time()


def idle_watchdog():
    """没人用就自己退出 —— 否则服务会一直挂在那儿没人管。"""
    limit = SRV["idle_shutdown_minutes"] * 60
    while not SHUTDOWN.is_set():
        if SHUTDOWN.wait(20):
            return
        idle = time.time() - LAST_HIT[0]
        if idle > limit:
            print("\n[自动退出] 已经 %d 分钟没有任何操作，服务关闭。" % SRV["idle_shutdown_minutes"])
            print("           下次要用，重新双击 start.bat 就行。")
            os._exit(0)


def expand_dir(path, cap, top):
    """把一个文件夹拆开看里面有什么 —— 用来解释"折叠"是怎么回事。

    ★ 2026-09-20 修：原实现是 os.walk + os.path.islink，这两个对 Windows 目录联接
      （junction）都失效 —— os.walk 把 junction 当普通目录走进去，islink 又拦不住。
      后果：主扫描显示 0 个文件的地方，「点开看里面」显示 20,000 个文件、体积比这一行
      本身还大。同一个东西给出两个数字，用户看一眼就不信了。
      现在跟 collect_files 用同一套遍历：scandir + 查「重解析点」那一位。
    """
    if not os.path.isdir(path):
        return {"ok": False, "error": "不是目录"}
    root = norm_root(path)
    cut = len(root) + 1
    files = []
    links_skipped = 0
    other_skipped = 0
    errors = 0
    stack = [root]
    # ★ 二轮修：多探一个再判「有没有被截断」。原来用 len(files) >= cap 判定，
    #   文件数恰好等于 cap 时会误报「文件太多，只统计了前 N 个」——
    #   这句会直接显示在录屏画面上，是自打脸。多扫 1 个的开销可以忽略。
    probe = cap + 1
    while stack and len(files) < probe:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    if len(files) >= probe:
                        break
                    try:
                        if e.is_dir(follow_symlinks=False):
                            # 同 collect_files：junction 的属性里也带 DIRECTORY，
                            # 光看 is_dir(follow_symlinks=False) 拦不住，必须看重解析位。
                            if getattr(e.stat(follow_symlinks=False),
                                       "st_file_attributes", 0) & ATTR_REPARSE:
                                links_skipped += 1
                                continue
                            rel = e.path[cut:] if len(e.path) > cut else e.name
                            if should_skip(e.name, rel, depth_of(rel)):
                                continue
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            if is_reserved(e.name):
                                continue
                            info = e.stat(follow_symlinks=False)
                            files.append((e.path, info.st_size, info.st_mtime))
                        else:
                            # ★ 二轮修：既不是目录也不是普通文件的东西（文件级符号
                            #   链接、悬空链接、socket 等）原来直接掉进缝里 —— 不计
                            #   体积、不计 links_skipped、也不计 errors，等于凭空消失。
                            #   collect_files 至少把它们记进 other_skipped。
                            other_skipped += 1
                    except OSError:
                        errors += 1
        except OSError:
            errors += 1
    capped = len(files) > cap
    if capped:
        files = files[:cap]
    total = sum(f[1] for f in files)
    files.sort(key=lambda x: -x[1])
    return {"ok": True, "path": path, "count": len(files), "total_size": total,
            "capped": capped, "links_skipped": links_skipped,
            "other_skipped": other_skipped, "errors": errors,
            "items": [{"name": os.path.basename(p),
                       "rel": os.path.relpath(p, root),
                       "size": s,
                       "mtime": fmt_date(m)}
                      for p, s, m in files[:top]]}


# ────────────────────────── 工具 ──────────────────────────
FMT_UNKNOWN = "未知"

# ★ 第三轮修（2026-09-20）：坏时间戳会把整场扫描带走。
#   现场：C:\Users\...\AppData\LocalLow\Tencent\WeType\Dict\**\*.bin 有 5 个微信输入法
#   词典文件，mtime = -11644318675.68624（约公元 1601 年）。Windows 的 CRT 不接受
#   负时间戳 —— 实测 time.localtime(-1) 就报 OSError: [Errno 22] Invalid argument，
#   gmtime 同样。全盘 135.8 万个文件里就这 5 个，代价却是 112 万文件的扫描全白跑
#   （卡在「正在归纳条目…」，然后整屏 traceback）。
#   坏时间戳还不只是"会崩"：它会污染聚合 —— lo = min(所有 mtime)，混进一个负数，
#   整个目录的「最老内容」就变成 1601 年，年代视图把微信输入法目录归进 1969 年前。
#   就算不崩，结论也是错的。
#   规矩：任何要变成日期的地方先过 safe_epoch；认不出就老实写「未知」，绝不抛。
_MTIME_MAX = 4102444800.0      # 2100-01-01。再往后就不像真实文件时间了，按坏值处理


def safe_epoch(ts):
    """把任意 stat 时间戳收敛成「有限、且 localtime 吃得下」的秒数；认不出返回 None。

    注意上限 2100 年是**我们的策略**，不是平台边界：实测本机 localtime 能吃到
    3001 年（32536799999），但那种值出现在真实文件上只能说明元数据坏了。宁可标
    「未知」，也不要拿它当真去算年代。
    """
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(t):        # nan / inf
        return None
    if t < 0.0 or t > _MTIME_MAX:
        return None
    return t


def fmt_date(ts):
    """epoch 秒 → "YYYY-MM-DD"。坏值一律 FMT_UNKNOWN，不抛异常。"""
    t = safe_epoch(ts)
    if t is None:
        return FMT_UNKNOWN
    try:
        return time.strftime("%Y-%m-%d", time.localtime(t))
    except (OSError, ValueError, OverflowError):
        # 各平台的接受区间不一致，兜底不能省 —— safe_epoch 只是快速路径
        return FMT_UNKNOWN


def fmt_size(n):
    for u, d in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= d:
            return "%.2f %s" % (n / d, u)
    return "%d B" % n


def idle_days(mtime):
    """闲置天数。时间戳不可信时返回 None —— 不能编一个数字出来。

    老实现直接算 (now - mtime)/86400，碰上那个 1601 年的值会得出 155488 天
    （15.5 万年），摆在界面上比不显示还糟。
    """
    t = safe_epoch(mtime)
    if t is None:
        return None
    return max(0.0, (time.time() - t) / 86400.0)


def is_reserved(name):
    return name.lower() in set(x.lower() for x in CFG["scan"]["reserved"])


def should_skip(name, relpath, depth=0):
    """relpath 是相对扫描根/浏览根的路径；depth=0 表示它就在根目录下面一层。"""
    if is_reserved(name):
        return True
    sc = CFG["scan"]
    low = name.lower()
    # 只在「根目录那一层」跳过的系统目录 —— 之前是所有层级都跳，会误伤同名的用户文件夹
    if depth <= 0 and any(low == s.lower() for s in sc["skip_at_root"]):
        return True
    lowp = relpath.lower().replace("/", "\\")
    return any(s.lower() in lowp for s in sc["skip_path_fragments"])


def depth_of(relpath):
    return max(0, len([x for x in relpath.replace("/", "\\").split("\\") if x]) - 1)


def privacy_hit(name):
    low = (name or "").lower()
    return any(p.lower() in low for p in CFG["privacy"]["name_patterns"])


# ────────────────────── 目录浏览（选择文件夹） ──────────────────────
def list_drives():
    out = []
    for L in string.ascii_uppercase:
        p = L + ":\\"
        if os.path.exists(p):
            out.append({"path": p, "name": L + ":"})
    return out


def list_dirs(path):
    """返回某个目录下的子目录（只列目录，不列文件）。"""
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return {"ok": False, "error": "不是有效目录: %s" % path}
    dirs = []
    stripped = path.rstrip("\\/")
    at_drive_root = (len(stripped) == 2 and stripped[1] == ":")
    try:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            try:
                if not os.path.isdir(full) or os.path.islink(full):
                    continue
            except OSError:
                continue
            if should_skip(name, name, 0 if at_drive_root else 1):
                continue
            dirs.append({"path": full, "name": name})
    except OSError as e:
        return {"ok": False, "error": "无法读取目录: %s" % e}
    parent = os.path.dirname(path.rstrip("\\/")) or None
    if parent and len(parent) < len(path):
        pass
    else:
        parent = None
    if path.rstrip("\\/").endswith(":"):
        parent = None
    return {"ok": True, "path": path, "parent": parent,
            "parts": path.rstrip("\\/").split("\\"), "dirs": dirs}


# ────────────────────────── 扫描 ──────────────────────────
# Windows 文件属性里的「重解析点」位：junction / 符号链接 / 云占位文件都会置这一位。
# 注意 os.path.islink() 对 junction 返回 False —— 光靠它拦不住。
ATTR_REPARSE = 0x400


def norm_root(p):
    """把扫描根规范化成一个稳定字符串。

    要小心的坑：`D:\\` 不能 rstrip 成 `D:` —— 那在 Windows 上是「D 盘当前目录」，
    os.scandir("D:") 根本不是根目录。所以盘符后面那个反斜杠必须留着。
    """
    p = (p or "").strip().strip('"')
    p = os.path.abspath(p) if p else p
    if p.endswith(":"):
        return p + "\\"
    if len(p) > 3:
        return p.rstrip("\\/")
    return p


def collect_files(root, on_tick=None, should_stop=None):
    """一次遍历把所有文件读进内存（path, size, mtime）。

    ★ 这里刻意不用 os.walk，两个原因（2026-09-20 在 D:\\ 实测，差 8 倍）：

      1) os.stat(path) vs scandir 的 DirEntry.stat()
         Windows 上 os.stat(路径) 每个文件都要 CreateFile + 查询 + Close，而且会被
         杀毒软件的实时防护逐个拦截 —— 实测 9,603 文件/秒。
         os.scandir 枚举目录时（FindFirstFile/FindNextFile）系统已经把大小和时间一并
         返回了，DirEntry.stat() 直接复用，不再发系统调用 —— 实测 115,002 文件/秒。

      2) os.walk 会走进 junction（Windows 目录联接）
         os.walk 用 is_dir()（默认解引用）判断，junction 被当成普通目录；而
         os.path.islink() 对 junction 返回 False，所以拦不住，于是走进 pnpm /
         node_modules 的联接网里反复重入同一批内容。
         用 scandir + is_dir(follow_symlinks=False)，链接天然不会被当成目录。

    不跟链接还顺带修了一个正确性问题：pnpm 把同一个包 junction 给多个项目，
    跟随就会把同一份内容重复计入体积；不跟随，它只在真实位置统计一次。

    返回 (files, truncated, aborted, stats)。
    """
    cap = CFG["scan"]["max_file_scan"]
    files = []
    truncated = False
    root = norm_root(root)
    cut = len(root) + 1                    # e.path 去掉 "root\" 前缀 = 相对路径
    stats = {"dirs": 0, "links_skipped": 0, "other_skipped": 0, "errors": 0,
             "bytes": 0, "slowest_ms": 0, "slowest_dir": None}
    last_tick = [0]                        # 每 1000 个文件回报一次进度
    t_scan = time.time()
    stack = [root]
    depth_cache = {}

    while stack:
        if should_stop and should_stop():
            return files, truncated, True, stats
        cur = stack.pop()
        stats["dirs"] += 1
        t_dir = time.perf_counter()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        # ★ 不要用 is_dir(follow_symlinks=False) 来拦链接 ——
                        #   Windows 上 junction 的属性里带 FILE_ATTRIBUTE_DIRECTORY，
                        #   所以它照样返回 True，走进去就在 pnpm 的联接网里重复遍历
                        #   （实测 D:\ 上 317 个 junction，全在 .pnpm-store 下面）。
                        #   必须显式看「重解析点」那一位。stat 走 DirEntry 缓存，不额外发系统调用。
                        if e.is_dir(follow_symlinks=False):
                            if getattr(e.stat(follow_symlinks=False), "st_file_attributes", 0) & ATTR_REPARSE:
                                stats["links_skipped"] += 1
                                continue
                            rel = e.path[cut:] if len(e.path) > cut else e.name
                            d = depth_cache.get(rel)
                            if d is None:
                                d = depth_cache[rel] = depth_of(rel)
                            if should_skip(e.name, rel, d):
                                continue
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            if is_reserved(e.name):
                                continue
                            info = e.stat(follow_symlinks=False)   # DirEntry 缓存，无额外系统调用
                            files.append((e.path, info.st_size, info.st_mtime))
                            stats["bytes"] += info.st_size
                            if len(files) >= cap:
                                truncated = True
                                break
                        else:
                            stats["other_skipped"] += 1
                    except OSError:
                        stats["errors"] += 1
        except OSError:
            stats["errors"] += 1

        dt_ms = (time.perf_counter() - t_dir) * 1000
        if dt_ms > stats["slowest_ms"]:
            stats["slowest_ms"] = round(dt_ms, 1)
            stats["slowest_dir"] = cur

        if truncated:
            break
        if on_tick and len(files) - last_tick[0] >= 1000:
            last_tick[0] = len(files)
            on_tick(len(files), cur, time.time() - t_scan)

    stats["slowest_ms"] = round(stats["slowest_ms"], 1)
    return files, truncated, False, stats


def build_entries(root, files, on_step=None, should_stop=None):
    """把文件列表压成"条目"：大文件夹折叠成一条，大文件单独一条。

    ★ 所有统计在一次遍历里算完（2026-09-20 重写）。
      原实现是「每遇到一个候选目录，就重扫一遍全部文件」来算它的扩展名分布 /
      子项体积 —— 复杂度 O(候选目录数 × 文件数)。D:\\ 扫出 56 万文件、折叠出几百个
      目录时，光这一项就是几亿次字符串比较，几十秒都跑不完。
      现在：先一次遍历把所有祖先的统计累加上去（O(文件数 × 目录深度)），
      再自底向上合并「最大的几个文件」（O(目录数)）。
    """
    sc = CFG["scan"]
    min_bytes = sc["min_size_mb"] * 1024 * 1024
    max_items = sc["max_items"]
    root = norm_root(root)

    def stopped():
        return bool(should_stop and should_stop())

    direct = defaultdict(list)
    for i, (p, s, m) in enumerate(files):
        direct[os.path.dirname(p)].append(i)

    # ── Pass A：每个文件只碰它自己所在的那一层目录（不爬祖先）──
    #   原来是「每个文件沿路径逐层累加」= O(文件数 × 目录深度)。node_modules /
    #   .pnpm 那种几十层的目录结构下，D:\AI_Projects 实测要跑 176 秒。
    node = {}

    def blank():
        return {"bytes": 0, "files": 0,
                "ext_n": defaultdict(int), "ext_b": defaultdict(int),
                "seg_b": defaultdict(int), "big": [], "lo": None, "hi": None}

    def nd(d):
        n = node.get(d)
        if n is None:
            n = node[d] = blank()
        return n

    root_len = len(root)
    for i, (p, s, m) in enumerate(files):
        if i % 50000 == 0 and stopped():
            return [], 0
        n = nd(os.path.dirname(p))
        e = os.path.splitext(p)[1].lower() or "(无扩展名)"
        n["bytes"] += s
        n["files"] += 1
        n["ext_n"][e] += 1
        n["ext_b"][e] += s
        n["seg_b"]["(根目录下的文件)"] += s     # 这一层自己的直接文件
        n["big"].append((s, os.path.basename(p)))
        # ★ 第三轮修：坏时间戳必须挡在聚合之外。
        #   混进一个负数，整个目录的「最老内容」就被拉到 1601 年，年代视图跟着错。
        ms = safe_epoch(m)
        if ms is not None:
            if n["lo"] is None or ms < n["lo"]:
                n["lo"] = ms
            if n["hi"] is None or ms > n["hi"]:
                n["hi"] = ms

    # ── 补全所有祖先节点（每个祖先只走一次，O(目录数)）──
    seen = set()
    for d in list(node):
        cur = d
        while cur not in seen:
            seen.add(cur)
            if cur == root:
                break
            up = os.path.dirname(cur)
            if up == cur or len(up) < root_len:
                break
            if up not in node:
                node[up] = blank()
            cur = up

    # ── Pass B：按深度从深到浅，把每个目录的统计并进父目录（O(目录数)）──
    for d in sorted(node, key=lambda x: -x.count("\\")):
        if d == root:
            continue
        up = os.path.dirname(d)
        if up == d or len(up) < root_len:
            continue
        a, b = node[d], nd(up)
        b["bytes"] += a["bytes"]
        b["files"] += a["files"]
        bn, be = b["ext_n"], b["ext_b"]
        for k, v in a["ext_n"].items():
            bn[k] += v
        for k, v in a["ext_b"].items():
            be[k] += v
        b["seg_b"][os.path.basename(d)] += a["bytes"]     # 子目录整体算作父目录的一个「第一层子项」
        if a["lo"] is not None and (b["lo"] is None or a["lo"] < b["lo"]):
            b["lo"] = a["lo"]
        if a["hi"] is not None and (b["hi"] is None or a["hi"] > b["hi"]):
            b["hi"] = a["hi"]
        if a["big"]:                                       # 来自子目录的大文件，补上相对路径前缀
            pfx = os.path.basename(d) + "\\"
            for sz, nm in a["big"]:
                b["big"].append((sz, pfx + nm))
            if len(b["big"]) > 8:
                b["big"] = heapq.nlargest(8, b["big"])

    # ── 目录树：parent → {子目录} ──
    #   ★ 必须用 node 的键（= 所有含文件的目录 + 它们的全部祖先）。
    #     老写法是从「含直接文件的目录」往上挂一层，于是像 D:\wechat 这种
    #     「自己不含文件、只含子目录」的中间层根本进不了树 —— 整棵子树会被静默丢掉。
    #     D 盘实测：wechat 58.94 GB + WeGameApps 57.11 GB 凭空消失，就是因为这个。
    children = defaultdict(set)
    for d in node:
        if d == root:
            continue
        up = os.path.dirname(d)
        if up == d or len(up) < root_len:
            continue
        children[up].add(d)

    def subtree_of(d):
        n = node.get(d)
        return (n["bytes"], n["files"]) if n else (0, 0)

    def sig_of(d):
        n = node.get(d)
        if n is None:
            return {"top_ext": [], "big_ext_by_count": [], "top_seg": [],
                    "biggest": [], "mtime_oldest": None, "mtime_newest": None}
        ext_n, ext_b = n["ext_n"], n["ext_b"]
        # ★ 扩展名按「占用体积」排，不按个数 —— 否则 .cl_cache×55、.lock×28 这类
        #   小文件会把真正装了几十 GB 的 .safetensors / .bin 挤出榜外，模型据此判成「缓存」。
        #   按个数的榜保留成辅助信号。
        return {"top_ext": [[e, ext_n[e], ext_b[e]]
                            for e, _ in sorted(ext_b.items(), key=lambda kv: -kv[1])[:6]],
                "big_ext_by_count": [[e, ext_n[e]]
                                     for e, _ in sorted(ext_n.items(), key=lambda kv: -kv[1])[:5]],
                "top_seg": [[k, v] for k, v in sorted(n["seg_b"].items(), key=lambda kv: -kv[1])[:7]],
                "biggest": [[nm, sz] for sz, nm in heapq.nlargest(8, n["big"])],
                "mtime_oldest": n["lo"], "mtime_newest": n["hi"]}

    def entry_dir(d, total, cnt):
        sig = sig_of(d)
        try:
            mt = safe_epoch(os.stat(d).st_mtime)
        except OSError:
            mt = None
        # ★ 目录自身的 mtime 只反映"最近有没有新增文件"，对缓存类目录完全没有区分度。
        #   所以折叠成一条的目录，额外记下它内部最老文件的时间 —— 年代视图用这个。
        #   sig 里的 lo 已在 Pass A 过滤掉坏值，这里直接采信；为 None 才退回目录自身 mtime。
        #   （不能用 `lo or mt`：lo == 0 是合法时间戳，被 or 当成假值会误退。）
        lo = sig.get("mtime_oldest")
        oldest = lo if lo is not None else mt
        idle = idle_days(oldest)
        return {"path": d, "name": os.path.basename(d) or d, "size": total,
                "files": cnt, "mtime": fmt_date(mt),
                "mtime_epoch": mt,
                "mtime_oldest": fmt_date(oldest),
                "mtime_oldest_epoch": oldest,
                "idle_days_oldest": (round(idle) if idle is not None else None),
                "privacy": privacy_hit(os.path.basename(d)),
                "sig": sig, "is_dir": True}

    def entry_file(p, s, m):
        ms = safe_epoch(m)
        idle = idle_days(ms)
        return {"path": p, "name": os.path.basename(p), "size": s, "files": 1,
                "mtime": fmt_date(ms),
                "mtime_epoch": ms,
                "mtime_oldest": fmt_date(ms),
                "mtime_oldest_epoch": ms,
                "idle_days_oldest": (round(idle) if idle is not None else None),
                "privacy": privacy_hit(os.path.basename(p)),
                "sig": {"top_ext": [[os.path.splitext(p)[1].lower() or "(无扩展名)", 1, s]],
                        "big_ext_by_count": [[os.path.splitext(p)[1].lower() or "(无扩展名)", 1]],
                        "top_seg": [[os.path.basename(p), s]],
                        "biggest": [[os.path.basename(p), s]]},
                "is_dir": False}

    def degraded_entry(path, total, cnt, is_dir):
        """兜底条目：体积和文件数照留，只有时间标「未知」。

        ★ 第三轮修：单条构造出意外，不该让 112 万文件的扫描白跑。丢条目等于凭空
          少一块体积，用户还以为自己盘里就这些 —— 降级保留比丢掉诚实得多。
        """
        try:
            pv = privacy_hit(os.path.basename(path))
        except Exception:
            # 兜底里再兜一层：连隐私标记都算不出来的话，宁可不标，也不能二次抛。
            pv = False
        return {"path": path, "name": os.path.basename(path) or path,
                "size": total, "files": cnt,
                "mtime": FMT_UNKNOWN, "mtime_epoch": None,
                "mtime_oldest": FMT_UNKNOWN, "mtime_oldest_epoch": None,
                "idle_days_oldest": None,
                "privacy": pv,
                "sig": {}, "is_dir": is_dir, "time_unknown": True}

    def entry_dir_safe(d, total, cnt):
        try:
            return entry_dir(d, total, cnt)
        except Exception:
            return degraded_entry(d, total, cnt, True)

    def entry_file_safe(p, s, m):
        try:
            return entry_file(p, s, m)
        except Exception:
            return degraded_entry(p, s, 1, False)

    entries = []
    max_collapse = sc.get("collapse_max_gb", 4) * 1024 ** 3
    min_files = sc["collapse_min_files"]
    ratio = sc["collapse_ratio"]

    def add_files(d):
        for i in direct.get(d, []):
            if files[i][1] >= min_bytes and len(entries) < max_items:
                entries.append(entry_file_safe(*files[i]))

    # ★ 按「体积」优先遍历，不按字母序。
    #   老实现是深度优先 + 先到先得：条目数一凑满 max_items，后面所有目录直接 return。
    #   而遍历顺序是字母序 —— D 盘最大的 D:\wechat（58.94 GB）恰好排在最后，
    #   配额早被前面一堆小目录吃光，于是全场最大的东西从结果里凭空消失，
    #   而 800 MB 的 dshbuild 反而榜上有名。
    #   改成最大堆：永远先处理当前体积最大的目录，配额一定花在最值得看的东西上。
    heap = []

    def push(d):
        total, _ = subtree_of(d)
        if total >= min_bytes:
            heapq.heappush(heap, (-total, d))

    add_files(root)
    for cd in children.get(root, ()):
        push(cd)

    while heap and len(entries) < max_items and not stopped():
        neg, d = heapq.heappop(heap)
        total = -neg
        cnt = subtree_of(d)[1]
        biggest = max((files[i][1] for i in direct.get(d, [])), default=0)
        if cnt >= min_files and total >= ratio * max(biggest, 1):
            kids = children.get(d)
            # 又大又碎、而且还能往下拆 → 折成一行对用户毫无意义（58.94 GB 一行
            # 看不出里面是什么），拆一层。但子项若全都太小，就折成一行，别把体积弄丢。
            if total > max_collapse and kids and any(subtree_of(cd)[0] >= min_bytes for cd in kids):
                add_files(d)
                for cd in kids:
                    push(cd)
                continue
            entries.append(entry_dir_safe(d, total, cnt))
            continue
        # 不够碎（被某个超大单文件主导）→ 不折叠，拆开
        add_files(d)
        for cd in children.get(d, ()):
            push(cd)

    entries.sort(key=lambda e: -e["size"])
    entries = entries[:max_items]
    if on_step:
        on_step(len(entries))
    return entries, sum(e["size"] for e in entries)


# ────────────────────────── JEV 判定 ──────────────────────────
def describe_short(i, e):
    """短描述：第 1 关（认名字）和第 4 关（按组复核）不需要完整的内部构成。"""
    kind = "目录" if e.get("is_dir") else "文件"
    return ("候选 %d  [%s] 名称=%s | 路径=%s | 体积=%s | 文件数=%d | 最后改动=%s") % (
        i + 1, kind, e["name"], e["path"], fmt_size(e["size"]), e["files"],
        e["mtime"])


def _pair(row):
    """兼容老/新两种 sig 格式，统一成 (名字, 字节) 或 (名字, 个数, 字节)。"""
    if isinstance(row, (list, tuple)):
        return row
    return (str(row),)


def describe(i, e):
    sig = e.get("sig") or {}
    # 按体积排的扩展名 —— 这是判断「几十 GB 到底存的是什么」最关键的信号
    by_size = []
    for row in (sig.get("top_ext") or [])[:5]:
        r = _pair(row)
        if len(r) >= 3:
            by_size.append("%s %s（%d 个）" % (r[0], fmt_size(r[2]), r[1]))
        elif len(r) == 2:
            by_size.append("%s（%d 个）" % (r[0], r[1]))
    by_cnt = ["%s×%d" % tuple(_pair(x)[:2]) for x in (sig.get("big_ext_by_count") or [])[:4]]
    # 第一层子项 + 最大的文件：都要带体积和名字
    segs = []
    for row in (sig.get("top_seg") or [])[:6]:
        r = _pair(row)
        if len(r) >= 2:
            segs.append("%s %s" % (r[0], fmt_size(r[1])))

    def _big(row):
        r = _pair(row)
        if len(r) >= 2:
            return "%s (%s)" % (r[0], fmt_size(r[1]))
        return str(r[0])

    big = [_big(x) for x in (sig.get("biggest") or [])[:5]]
    kind = "目录" if e.get("is_dir") else "文件"
    return ("候选 %d  [%s] 名称=%s | 路径=%s | 体积=%s | 文件数=%d | 最后改动=%s\n"
            "        里面各占多少：%s\n"
            "        扩展名（按占用体积）：%s\n"
            "        最多的扩展名（按个数）：%s\n"
            "        体积最大的几个文件：%s") % (
        i + 1, kind, e["name"], e["path"], fmt_size(e["size"]), e["files"],
        e["mtime"],
        "; ".join(segs) or "无",
        ", ".join(by_size) or "未知",
        ", ".join(by_cnt) or "未知",
        "; ".join(big) or "无")


def ask_jev(questions, lines, stop=None):
    """发一次请求。questions 的编号必须与 lines 里的「候选 N」编号一一对应。

    ★ 2026-09-20：加了 stop 回调。原来超时是 240 秒，点「中止」后要等正在飞的那次请求
      自己超时才算完 —— 表现就是界面卡在「正在中止…」好几分钟。现在超时收到 20 秒，
      而且超时/出错后先问一句"还继续吗"，用户点了中止就不再重试。
    """
    g = CFG["gates"]
    state = g["state_prefix"] + "\n\n" + "\n".join(lines)
    body = json.dumps({"state": state, "model": MODEL, "questions": q_ok(questions)},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + API_KEY)
    req.add_header("Content-Type", "application/json")
    b = CFG["batch"]
    last = None
    for attempt in range(1, b["retries"] + 1):
        if stop and stop():
            raise RuntimeError("已中止")
        try:
            with urllib.request.urlopen(req, timeout=b["timeout_sec"]) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:200])
            if stop and stop():
                raise RuntimeError("已中止")
            if e.code in (429, 529) and attempt < b["retries"]:
                time.sleep(int(e.headers.get("retry-after") or 2 * attempt))
                continue
            break
        except Exception as e:
            last = repr(e)
            if stop and stop():
                raise RuntimeError("已中止")
            if attempt < b["retries"]:
                time.sleep(1.5 * attempt)
                continue
            break
    raise RuntimeError(last or "unknown error")


def q_ok(questions):
    """校验问题包：类型合法、非空，防止把垃圾直接发给 API。"""
    out = {}
    for k, v in questions.items():
        t = v.get("type")
        if t not in ("choice", "score", "noul"):
            raise ValueError("非法问题类型: %r (%s)" % (t, k))
        if not v.get("instructions"):
            raise ValueError("问题 %s 缺 instructions" % k)
        out[str(k)] = v
    if not out:
        raise ValueError("空问题包")
    return out


# ────────────────────────── 打分与定档 ──────────────────────────
def _num(x, default=0.0):
    """把模型回包里的数值字段收敛成 float。

    ★ 2026-09-20 修：模型偶尔不回 confidence（或回个字符串）。原来 None 会一路传下去，
      到 score_entry 里 min(1.0, None) 直接 TypeError —— 整个任务作废。
      缺字段就按「零把握」处理：结论会被压到「慎重」，倒向安全那一侧。
    """
    if isinstance(x, bool):
        # ★ 二轮修：True/False 不是数值。float(True)==1.0 会被当成「满把握」——
        #   方向恰好是危险的（把握越高越少让人复核）。无法解释的输入一律按 default。
        return default
    try:
        f = float(x)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        # ★ 二轮修：NaN 和 inf 一起挡。inf 更阴 —— json.dumps 会写出裸的 Infinity，
        #   那不是合法 JSON，浏览器 JSON.parse 直接抛错，整个界面白屏。
        return default
    return f


def _conf01(x, default=0.0):
    """把「把握度」收敛到 [0,1]。

    ★ 二轮修：把握度和损失分是**两套量纲**（0~1 vs 0~3），所以范围检查不能塞进
      _num 里 —— 那会把 3.0 分的损失分一起打成 0，整个打分逻辑静默失效。
      越界的值（比如百分制的 90）不猜、也不截断成 1.0（1.0 是「最有把握」，
      方向正好是危险的），按 default 处理，倒向安全侧。
    """
    f = _num(x, None)
    if f is None or f < 0.0 or f > 1.0:
        return default
    return f


def _dict_or_none(x):
    """概率分布：不是字典就当没有（模型回字符串时，前端 Object.keys 会给出乱码）。"""
    return x if isinstance(x, dict) else None


def score_entry(e, kind, conf, d_raw):
    w = CFG["weights"]
    r = CFG["rules"]
    conf = _conf01(conf)
    d_raw = _num(d_raw)
    sh = w["shrink_floor"] + (1 - w["shrink_floor"]) * max(0.0, min(1.0, conf))
    idle = idle_days(e["mtime_epoch"])
    # ★ 第三轮修：时间戳不可信时 idle 是 None。既不能拿它做除法（TypeError），
    #   也不该给「越老越该删」的年龄加成 —— 未知一律按中性 1.0 处理，不猜。
    age_norm = 0.0 if idle is None else min(1.0, idle / w["age_full_days"])
    ag = 1.0 + w["age_max"] * age_norm
    raw = d_raw * sh * ag
    cap = r["kind_num_cap"].get(kind, 3.0)
    if conf < r["low_conf_threshold"]:
        cap = min(cap, r["low_conf_cap"])
    return round(min(raw, cap), 3), round(sh, 3), round(ag, 3)


TIER_ORDER = {"建议删除": 0, "可以考虑": 1, "慎重": 2, "别碰": 3}
RAW_TIER = ["别碰", "慎重", "可以考虑", "建议删除"]


def verdict_of(kind, conf, d_raw, kind_conf=None):
    """给结论定档。conf 传的是「这一行最弱的那个置信度」(row_conf)。

    ★ 2026-09-20 修：原来只拿「删除损失」的置信度来降级，**完全没管「类别」的置信度**。
      后果：模型连「这是什么东西」都只有 0.45 把握时，结论文案照样敢写「可以考虑删」。
      实测 trigger：E:\\whisper_models 37GB —— 修输入后 kind_conf 从 0.78 掉到 0.45，
      结论却纹丝不动。连是什么都不知道，凭什么建议删。
    """
    r = CFG["rules"]
    conf = _conf01(conf)
    d_raw = _num(d_raw)
    kind_conf = _conf01(kind_conf, None)
    base = RAW_TIER[int(round(max(0, min(3, d_raw))))]
    cap = r["verdict_cap_by_kind"].get(kind, "建议删除")
    floor = r.get("kind_conf_floor")
    if floor is not None and (kind_conf is None or kind_conf < floor):
        # 连「这是什么」都没把握 → 一律不许进「可以考虑」以上
        # ★ 二轮修：原来是 `kind_conf is not None and ...`，即「把握度无效时闸门
        #   直接跳过」—— 把「不知道」当成了「没问题」，与上一行注释的意图正好相反。
        #   实测：模型回 confidence="high"（字符串）时，结论能一路走到「建议删除」。
        cap = min([cap, "慎重"], key=lambda x: -TIER_ORDER[x])
    if conf < r["low_conf_threshold"]:
        cap = min([cap, "慎重"], key=lambda x: -TIER_ORDER[x])
    elif conf < r["mid_conf_threshold"] and TIER_ORDER[base] < TIER_ORDER["可以考虑"]:
        base = "可以考虑"
    return base if TIER_ORDER[base] >= TIER_ORDER[cap] else cap


# ────────────────────────── 四关编排（学 jev-review 的分阶段门控） ──────────────────────────
def _mag(n):
    try:
        n = max(int(n or 0), 1)
    except Exception:
        n = 1
    return min(int(math.log10(n)), 12)


def sig_key(e):
    """特征签名。同签名的对象只判一次（学 pg_typesafe：1000 条去重成 38 种再调 API）。"""
    if not CFG["rules"].get("dedup", True):
        return ("solo", e.get("path"))
    sig = e.get("sig") or {}

    def cb(n):
        return 0 if n < 3 else 1 if n < 12 else 2 if n < 50 else 3

    exts = tuple((x[0], cb(x[1])) for x in (sig.get("top_ext") or [])[:5])
    return (bool(e.get("is_dir")), _mag(e.get("size")), _mag(e.get("files")), exts)


def build_classes(entries):
    """聚成等价类；代表取组里最大的那个（最大件先出结论，观感好）。"""
    idx = {}
    classes = []
    for i, e in enumerate(entries):
        k = sig_key(e)
        if k not in idx:
            idx[k] = {"rep": i, "members": [i]}
            classes.append(idx[k])
        else:
            c = idx[k]
            c["members"].append(i)
            if e.get("size", 0) > entries[c["rep"]].get("size", 0):
                c["rep"] = i
    classes.sort(key=lambda c: -entries[c["rep"]].get("size", 0))
    return classes


def judge_pipeline(job, entries):
    """四关编排。返回 (items, stats)。

    第 1 关  Noul   认名字 —— 认不出的直接「看不懂」，后面不再问（省钱 + 杜绝瞎猜）
    第 2 关  Choice 是什么类别 —— 程序本体/应用状态/用户产出到此定档「别碰」
    第 3 关  Score  删除损失多大 —— 只对可能是垃圾的问
    第 4 关  Choice  同签名的一组，处理方式是否一致 —— 类别级决策
    """
    g, r, b = CFG["gates"], CFG["rules"], CFG["batch"]
    usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
    job["usage"] = dict(usage)          # 一个请求都没发成时，前端也得拿到 usage
    ulock = threading.Lock()

    def note(res):
        with ulock:
            u = res.get("usage") or {}
            usage["input_tokens"] += u.get("input_tokens", 0)
            usage["output_tokens"] += u.get("output_tokens", 0)
            usage["requests"] += 1
            job["usage"] = dict(usage)

    classes = build_classes(entries)
    failed = {}          # {类下标: 失败原因} —— 这一批没判出来，但绝不能让它在结果里消失
    gate = [0] * len(entries)
    kind = [None] * len(entries)
    kind_conf = [None] * len(entries)
    kind_prob = [None] * len(entries)
    d_raw = [None] * len(entries)
    d_conf = [None] * len(entries)
    d_prob = [None] * len(entries)
    blocked1 = [0]        # 严格统计「第 1 关就没认出来」的类数（第 2 关答 unknown 的不算）

    def chunks(lst, n):
        return [lst[i:i + n] for i in range(0, len(lst), n)]

    def round_run(ci_list, build_q, on_res, label, short=False):
        """ci_list 是类下标；对每批的代表并发提问。short=True 只送短描述（省 token）。"""
        if not ci_list or job.get("cancel"):
            return
        done = [0]
        lock = threading.Lock()
        desc = describe_short if short else describe

        def mark_failed(ci_batch, why):
            """★ 单批失败 = 这一批降级，不是整个任务作废。

            原来这里直接 raise，异常经 ex.map 冒到 run_job 的兜底 except ——
            已经判好的几百条结论一个字都不留，用户只看到一句报错。
            现在记下来、继续跑其它批，失败的类在结果里以「慎重 / 需人工」露头。
            """
            with ulock:
                for ci in ci_batch:
                    failed.setdefault(ci, (why or "")[:200])
                job["degraded"] = len(failed)

        def work(ci_batch):
            if job.get("cancel"):
                return
            try:
                qs = build_q(ci_batch)
                lines = [desc(j, entries[classes[ci]["rep"]]) for j, ci in enumerate(ci_batch)]
                res = ask_jev(qs, lines, stop=lambda: bool(job.get("cancel")))
                if job.get("cancel"):
                    return
                note(res)
                on_res(ci_batch, res)
            except RuntimeError as e:
                if "已中止" in str(e):
                    return
                mark_failed(ci_batch, str(e))
                return
            except Exception as e:
                mark_failed(ci_batch, repr(e))
                return
            with lock:
                done[0] += len(ci_batch)
                job["phase"] = "%s %d/%d" % (label, done[0], len(ci_list))

        with concurrent.futures.ThreadPoolExecutor(max_workers=b["workers"]) as ex:
            list(ex.map(work, chunks(ci_list, b["size"])))

    # ── 第 1 关：认名字 ──
    def q1(ci_batch):
        return {"r%d" % j: {"type": "noul", "instructions": g["recognize"]}
                for j in range(len(ci_batch))}

    def on1(ci_batch, res):
        for j, ci in enumerate(ci_batch):
            rep = classes[ci]["rep"]
            gate[rep] = 1
            a = res["answers"].get("r%d" % j, {})
            v = _conf01(a.get("noul"), 0.0)
            if v < g.get("recognize_floor", 0.5):
                kind[rep] = "unknown"
                kind_conf[rep] = round(v, 4)
                blocked1[0] += 1

    round_run(list(range(len(classes))), q1, on1, "第1关/4 · 认名字", short=True)

    # ── 第 2 关：类别 ──
    pass1 = [ci for ci, c in enumerate(classes)
             if kind[c["rep"]] is None and ci not in failed]

    def q2(ci_batch):
        return {"k%d" % j: {"type": "choice", "instructions": g["kind"] % (j + 1),
                            "criteria": CFG["criteria"]["kinds"]}
                for j in range(len(ci_batch))}

    def on2(ci_batch, res):
        for j, ci in enumerate(ci_batch):
            rep = classes[ci]["rep"]
            gate[rep] = 2
            a = res["answers"].get("k%d" % j, {})
            kind[rep] = a.get("choice") or "unknown"
            kind_conf[rep] = _conf01(a.get("confidence"), 0.0)
            kind_prob[rep] = _dict_or_none(a.get("probabilities"))

    round_run(pass1, q2, on2, "第2关/4 · 判类别")

    # ── 第 3 关：删除损失（只对「可能是垃圾」的问） ──
    loss_kinds = set(r.get("loss_kinds") or [])
    pass2 = [ci for ci in pass1 if kind[classes[ci]["rep"]] in loss_kinds]

    def q3(ci_batch):
        return {"d%d" % j: {"type": "score", "instructions": g["loss"] % (j + 1),
                            "criteria": CFG["criteria"]["deletability"]}
                for j in range(len(ci_batch))}

    def on3(ci_batch, res):
        for j, ci in enumerate(ci_batch):
            rep = classes[ci]["rep"]
            gate[rep] = 3
            a = res["answers"].get("d%d" % j, {})
            d_raw[rep] = _num(a.get("score"), None)
            d_conf[rep] = _conf01(a.get("confidence"), 0.0)
            d_prob[rep] = _dict_or_none(a.get("probabilities"))

    round_run(pass2, q3, on3, "第3关/4 · 问损失")

    # ── 第 4 关：同签名一组，处理方式是否一致（类别级决策） ──
    multi = [ci for ci, c in enumerate(classes)
             if len(c["members"]) >= 2 and kind[c["rep"]] not in (None, "unknown")]
    class_choice = {}

    def q4(ci_batch):
        qs = {}
        for j, ci in enumerate(ci_batch):
            mem = classes[ci]["members"]
            names = "、".join(entries[i]["name"] for i in mem[:6])
            if len(mem) > 6:
                names += " 等 %d 个" % len(mem)
            qs["c%d" % j] = {"type": "choice",
                             "instructions": g["batch"] % (len(mem), names),
                             "criteria": g["batch_options"]}
        return qs

    def on4(ci_batch, res):
        for j, ci in enumerate(ci_batch):
            a = res["answers"].get("c%d" % j, {})
            class_choice[ci] = {"choice": a.get("choice"),
                                "confidence": _conf01(a.get("confidence"), None),
                                "probabilities": _dict_or_none(a.get("probabilities"))}
            gate[classes[ci]["rep"]] = 4

    round_run(multi, q4, on4, "第4关/4 · 按组复核", short=True)

    # ── 汇总：代表结论映射回全部成员 ──
    items = []
    degraded_items = 0
    for ci, c in enumerate(classes):
        rep = c["rep"]
        k, kc = kind[rep], kind_conf[rep]
        why = failed.get(ci)
        if k is None:
            if why is None:
                continue                               # 既没结论也没有失败记录 = 被中止截断
            k = "unknown"                              # 这一批请求挂了 → 降级，但必须露头
        if why:
            degraded_items += 1
        dr, dc, dp, kp = d_raw[rep], d_conf[rep], d_prob[rep], kind_prob[rep]
        gt = gate[rep]
        cc = class_choice.get(ci)

        # 类别摇摆（学 neo4jev：分布本身就是信号）
        swing = None
        if kp:
            try:
                pr = sorted(kp.items(), key=lambda x: -x[1])
                if (len(pr) >= 2 and (pr[0][1] - pr[1][1]) < r.get("swing_gap", 0.2)
                        and pr[1][1] >= r.get("swing_top2_min", 0.25)):
                    swing = [pr[0][0], pr[1][0], round(pr[0][1], 3), round(pr[1][1], 3)]
            except Exception:
                swing = None

        if dr is None:
            # 第 2 关就定档的（程序本体/应用状态/用户产出/看不懂），没问过损失分
            d_final = sh = ag = None
            if k == "unknown":
                verdict = "慎重"
            else:
                floor = r.get("kind_conf_floor")
                # ★ 二轮修：这一行原来直接拿 kc 比大小。kc 若是字符串（模型回
                #   "0.9"），str >= float 抛 TypeError —— 而这个异常出在**汇总阶段**，
                #   不在 work() 的 try 里，上一轮加的「单批容错」根本兜不住，
                #   整个任务照样作废。
                _kc01 = _conf01(kc, None)
                ok = _kc01 is not None and _kc01 >= (floor if floor is not None else 0.5)
                verdict = "别碰" if ok else "慎重"
            row_conf = _conf01(kc, 0.0)
        else:
            d_final, sh, ag = score_entry(entries[rep], k, dc, dr)
            # ★ 二轮修：原来是「谁有值就取谁」。字段无效/缺失时被静默剔除，
            #   等于把「不知道类别的把握度」当成「不参与判断」——乐观方向。
            #   现在任一无效都算零把握，倒向需要人工确认那一侧。
            row_conf = min(_conf01(dc, 0.0), _conf01(kc, 0.0))
            verdict = verdict_of(k, row_conf, dr, kc)

        for i in c["members"]:
            e = entries[i]
            idle = idle_days(e["mtime_epoch"])
            items.append({
                "path": e["path"], "name": e["name"], "size": e["size"],
                "files": e["files"], "mtime": e["mtime"],
                "mtime_oldest": e.get("mtime_oldest"),
                "idle_days": (round(idle) if idle is not None else None),
                "idle_days_oldest": e.get("idle_days_oldest"),
                "is_dir": e["is_dir"], "privacy": e.get("privacy", False),
                "kind": k, "kind_conf": _conf01(kc, None), "kind_prob": _dict_or_none(kp),
                "d_raw": _num(dr, None), "conf": _conf01(dc, None),
                "row_conf": round(row_conf, 4),
                "d_prob": _dict_or_none(dp), "shrink": sh, "age_boost": ag, "d_final": d_final,
                "verdict": verdict,
                "needs_human": (row_conf < 0.4) or k == "unknown",
                "gate": gt, "swing": swing, "degraded": why,
                "class_size": len(c["members"]), "is_rep": (i == rep),
                "class_choice": cc,
            })

    items.sort(key=lambda x: (TIER_ORDER[x["verdict"]], -(x["d_final"] or 0), -x["size"]))
    _whys = sorted({w for w in failed.values() if w})
    stats = {"entries": len(entries), "classes": len(classes),
             "dedup_saved": len(entries) - len(classes),
             "gate1_blocked": blocked1[0],
             "gate3_asked": len(pass2), "gate4_asked": len(multi),
             "requests": usage["requests"],
             "degraded": len(failed), "degraded_items": degraded_items,
             # ★ 二轮修：光有计数不够。key 失效 / 断网 / 服务挂 三种故障长得一模一样，
             #   得把「为什么没判出来」带出去，否则用户只能靠猜。
             "degraded_why": _whys[:3],
             # 每一类都失败 = 不是个别批次抽风，是环境问题 —— 任务层要报错而不是报「完成」
             "all_failed": bool(classes) and len(failed) >= len(classes)}
    return items, stats


# ────────────────────────── 任务 ──────────────────────────
def run_job(job, root):
    try:
        job["state"] = "scanning"
        job["phase"] = "正在读取文件列表…"
        t0 = time.time()

        def tick(n, cur="", elapsed=0.0):
            job["files_seen"] = n
            rate = int(n / elapsed) if elapsed > 0.05 else 0
            job["scan_rate"] = rate
            job["phase"] = "正在读文件：%s 个%s%s" % (
                format(n, ","),
                ("　%s/秒" % format(rate, ",")) if rate else "",
                ("　当前：" + cur[-52:]) if cur else "")

        files, truncated, aborted, scan_stats = collect_files(
            root, tick, lambda: bool(job.get("cancel")))
        job["file_count"] = len(files)
        job["truncated"] = truncated
        job["scan_stats"] = scan_stats
        if aborted:
            job["state"] = "cancelled"
            job["phase"] = "已中止（%s）· 读到 %s 个文件" % (
                job.get("cancel_why", "用户中止"), format(len(files), ","))
            job["scan_ms"] = int((time.time() - t0) * 1000)
            return
        job["phase"] = "正在归纳条目…"

        entries, total_size = build_entries(root, files, should_stop=lambda: bool(job.get("cancel")))
        if job.get("cancel"):
            job["state"] = "cancelled"
            job["phase"] = "已在「归纳条目」阶段中止"
            job["scan_ms"] = int((time.time() - t0) * 1000)
            return
        job["total_entries"] = len(entries)
        job["total_size"] = total_size
        job["phase"] = "开始判断（共 %d 个条目）…" % len(entries)
        job["state"] = "judging"
        job["scan_ms"] = int((time.time() - t0) * 1000)
        job["entries"] = entries          # 留给「按新标准重判」复用，不必重扫

        items, stats = judge_pipeline(job, entries)
        job["items"] = items
        job["dedup"] = stats
        job["judged"] = len(items)

        if job.get("cancel"):
            job["state"] = "cancelled"
            job["phase"] = "已中止（%s）· 保留已判断的 %d 条" % (
                job.get("cancel_why", "用户中止"), len(items))
            return
        if stats.get("all_failed"):
            # ★ 二轮修：一次请求都没成功，不该显示成「完成」。
            #   注意顺序 —— 先判用户主动中止，再判全挂：中止不算故障。
            job["state"] = "error"
            job["error"] = "JEV 一次都没调用成功，%d 条都没判出来：%s" % (
                len(items), (stats.get("degraded_why") or ["原因未知"])[0])
            job["phase"] = "全部失败 · %d 条没判出来" % len(items)
            return
        job["state"] = "done"
        job["phase"] = "完成 · %d 条归并成 %d 类，只判了 %d 次" % (
            stats["entries"], stats["classes"], stats.get("requests", 0))
        job["elapsed_ms"] = int((time.time() - t0) * 1000)
        save_cache(root, job)
    except Exception as e:
        job["state"] = "error"
        job["error"] = "%s" % e
        job["trace"] = traceback.format_exc()[-1200:]


def run_judge_only(job):
    """「按新标准重判」：条目已在内存里，只重跑四关，不重扫磁盘。"""
    try:
        t0 = time.time()
        job["state"] = "judging"
        job["phase"] = "开始重判（共 %d 个条目）…" % len(job["entries"])
        items, stats = judge_pipeline(job, job["entries"])
        job["items"] = items
        job["dedup"] = stats
        job["judged"] = len(items)
        if job.get("cancel"):
            job["state"] = "cancelled"
            return
        if stats.get("all_failed"):
            job["state"] = "error"
            job["error"] = "JEV 一次都没调用成功，%d 条都没判出来：%s" % (
                len(items), (stats.get("degraded_why") or ["原因未知"])[0])
            job["phase"] = "重判全部失败 · %d 条没判出来" % len(items)
            return
        job["state"] = "done"
        job["phase"] = "重判完成"
        job["elapsed_ms"] = int((time.time() - t0) * 1000)
        save_cache(job["root"], job)
    except Exception as e:
        job["state"] = "error"
        job["error"] = "%s" % e
        job["trace"] = traceback.format_exc()[-1200:]


_CACHE_HEAD_RE = re.compile(
    r'"(root|created|file_count|total_size)"\s*:\s*("(?:[^"\\]|\\.)*"|null|-?[\d.eE+]+)')


def cache_meta(name):
    """把「这是哪一次扫描」从缓存文件里捡出来，供界面挑着回放。

    整个文件可能好几 MB，20 个全读进来太慢 —— 而 root / created / file_count /
    total_size 在 json.dump 时是排在最前面的几个键，读开头 2KB 足够。
    （读不到的字段一律给 None，界面自己兜底，不编。）"""
    out = {"file": name, "root": None, "created": None,
           "file_count": None, "total_size": None}
    try:
        with open(os.path.join(CACHE_DIR, name), encoding="utf-8") as f:
            head = f.read(2048)
    except Exception:
        return out
    found = {}
    for m in _CACHE_HEAD_RE.finditer(head):
        found.setdefault(m.group(1), m.group(2))
    if "root" in found:
        try:
            out["root"] = json.loads(found["root"])
        except Exception:
            out["root"] = found["root"].strip('"')
    if "created" in found:
        try:
            out["created"] = json.loads(found["created"])
        except Exception:
            out["created"] = found["created"].strip('"')
    for k in ("file_count", "total_size"):
        if k in found and found[k] != "null":
            try:
                out[k] = int(float(found[k]))
            except Exception:
                pass
    return out


def save_cache(root, job):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        name = time.strftime("%Y%m%d-%H%M%S") + ".json"
        with open(os.path.join(CACHE_DIR, name), "w", encoding="utf-8") as f:
            json.dump({"root": root, "model": MODEL, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "file_count": job.get("file_count"), "total_size": job.get("total_size"),
                       "items": job["items"], "entries": job.get("entries"),
                       "dedup": job.get("dedup"), "usage": job.get("usage")}, f, ensure_ascii=False)
        job["cache"] = name
    except Exception:
        pass


def apply_criteria(payload):
    """把界面上改的「标准」套进 CFG。白名单 + 类型校验，改坏了也不至于崩。"""
    applied, errors = [], []
    crit = payload.get("criteria") or {}

    if "kinds" in crit:
        try:
            incoming = dict(crit["kinds"])
            if not incoming:
                raise ValueError("空的类别选项")
            nk = dict(CFG["criteria"]["kinds"])          # 允许只改其中几项，其余保留
            for k, v in incoming.items():
                if k not in nk:
                    raise ValueError("未知类别: %s" % k)
                nk[k] = str(v)[:300]
            CFG["criteria"]["kinds"] = nk
            applied.append("类别选项(%d 项)" % len(incoming))
        except Exception as e:
            errors.append("类别选项: %s" % e)

    if "deletability" in crit:
        try:
            dl = [str(x)[:200] for x in list(crit["deletability"])[:8]]
            if len(dl) < 2:
                raise ValueError("至少要 2 档")
            CFG["criteria"]["deletability"] = dl
            applied.append("损失档位")
        except Exception as e:
            errors.append("损失档位: %s" % e)

    gates = payload.get("gates") or {}
    for key, cap in (("recognize", 500), ("kind", 200), ("loss", 500), ("batch", 500)):
        if key in gates:
            try:
                v = str(gates[key])[:cap]
                if not v.strip():
                    raise ValueError("不能为空")
                old = CFG["gates"].get(key, "")
                # 占位符校验：原文里有的 %d/%s 一个都不能丢，丢了要到运行时才炸
                for ph in ("%d", "%s"):
                    if old.count(ph) and not v.count(ph):
                        raise ValueError("这段问法必须保留占位符 %s" % ph)
                CFG["gates"][key] = v
                applied.append("问法·%s" % key)
            except Exception as e:
                errors.append("问法·%s: %s" % (key, e))

    for key in ("recognize_floor",):
        if key in gates:
            try:
                v = float(gates[key])
                if not 0.05 <= v <= 0.95:
                    raise ValueError("要在 0.05~0.95 之间")
                CFG["gates"][key] = v
                applied.append(key)
            except Exception as e:
                errors.append("%s: %s" % (key, e))

    for key in ("kind_conf_floor", "swing_gap", "swing_top2_min",
                "low_conf_threshold", "mid_conf_threshold"):
        if key in (payload.get("rules") or {}):
            try:
                v = float(payload["rules"][key])
                if not 0.0 <= v <= 1.0:
                    raise ValueError("要在 0~1 之间")
                CFG["rules"][key] = v
                applied.append(key)
            except Exception as e:
                errors.append("%s: %s" % (key, e))

    if "dedup" in (payload.get("rules") or {}):
        CFG["rules"]["dedup"] = bool(payload["rules"]["dedup"])
        applied.append("特征去重")

    return applied, errors


def save_config():
    """改标准后可选持久化。先备份旧文件，改砸了能一键回滚。"""
    import shutil
    if os.path.exists(CONFIG_PATH):
        shutil.copyfile(CONFIG_PATH, CONFIG_PATH + ".bak")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(CFG, f, ensure_ascii=False, indent=2)


def job_brief(j):
    return {"id": j["id"], "root": j.get("root"), "state": j.get("state"),
            "phase": j.get("phase"), "judged": j.get("judged"),
            "total": j.get("total_entries"), "files": j.get("files_seen"),
            "secs": round(time.time() - (j.get("t0") or time.time()), 1),
            "cache": j.get("cache")}


def running_jobs():
    return [j for j in JOBS.values() if j.get("state") in ("queued", "scanning", "judging")]


def cancel_job(j, why="用户中止"):
    j["cancel"] = True
    j["cancel_why"] = why
    return j


def prune_jobs(keep=25):
    """JOBS 会一直涨，只留最近若干个已结束的。"""
    done = [j for j in JOBS.values() if j.get("state") not in ("queued", "scanning", "judging")]
    done.sort(key=lambda j: j.get("t0") or 0)
    for j in done[:-keep]:
        JOBS.pop(j["id"], None)


# ────────────────────────── HTTP ──────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200, ctype="application/json; charset=utf-8"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        touch()
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        try:
            if p == "/api/ping":
                return self._send({"ok": True})
            if p == "/api/expand":
                return self._send(expand_dir(q.get("path", [""])[0],
                                             SRV["expand_max_files"], SRV["expand_top"]))
            if p in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self._send(f.read(), ctype="text/html; charset=utf-8")
            if p == "/api/info":
                return self._send({"ok": True, "home": HOME, "user": os.path.basename(HOME),
                                   "model": MODEL, "has_key": bool(API_KEY),
                                   "config": {"min_size_mb": CFG["scan"]["min_size_mb"],
                                              "max_items": CFG["scan"]["max_items"],
                                              "max_file_scan": CFG["scan"]["max_file_scan"],
                                              "collapse_min_files": CFG["scan"]["collapse_min_files"],
                                              "collapse_ratio": CFG["scan"]["collapse_ratio"],
                                              "collapse_max_gb": CFG["scan"].get("collapse_max_gb", 4),
                                              "criteria": CFG["criteria"],
                                              "weights": CFG["weights"],
                                              "rules": CFG["rules"],
                                              "gates": CFG["gates"],
                                              "ui": CFG.get("ui", {}),
                                              "privacy": CFG["privacy"]},
                                   "server": {"port": SRV["port"],
                                              "idle_shutdown_minutes": SRV["idle_shutdown_minutes"]}})
            if p == "/api/criteria":
                return self._send({"ok": True, "criteria": CFG["criteria"],
                                   "gates": CFG["gates"], "rules": CFG["rules"]})
            if p == "/api/drives":
                return self._send({"ok": True, "drives": list_drives(), "quick": [
                    {"path": os.path.join(HOME, x), "name": x}
                    for x in ["Desktop", "Downloads", "Documents", "Videos", "Pictures"]
                    if os.path.isdir(os.path.join(HOME, x))]})
            if p == "/api/browse":
                return self._send(list_dirs(q.get("path", [HOME])[0]))
            if p == "/api/job":
                jid = q.get("id", [""])[0]
                j = JOBS.get(jid)
                if not j:
                    return self._send({"ok": False, "error": "job 不存在"}, 404)
                since = int(q.get("since", ["0"])[0])
                with JOBS_LOCK:
                    items = j["items"][since:]
                    total = len(j["items"])
                return self._send({
                    "ok": True, "state": j["state"], "phase": j["phase"],
                    "error": j.get("error"), "trace": j.get("trace"),
                    "root": j["root"], "file_count": j.get("file_count"),
                    "truncated": j.get("truncated"), "total_entries": j.get("total_entries"),
                    "total_size": j.get("total_size"), "judged": j.get("judged"),
                    "scan_ms": j.get("scan_ms"), "elapsed_ms": j.get("elapsed_ms"),
                    "files_seen": j.get("files_seen"), "cancel_why": j.get("cancel_why"),
                    "scan_rate": j.get("scan_rate"), "scan_stats": j.get("scan_stats"),
                    "replaced": j.get("replaced"),
                    "cache": j.get("cache"), "cursor": total, "items": items,
                    "dedup": j.get("dedup"), "rejudge_of": j.get("rejudge_of"),
                    "degraded": j.get("degraded"),
                    "usage": j.get("usage")})
            if p == "/api/jobs":
                with JOBS_LOCK:
                    js = sorted(JOBS.values(), key=lambda j: j.get("t0") or 0, reverse=True)
                    return self._send({"ok": True, "running": len(running_jobs()),
                                       "jobs": [job_brief(j) for j in js[:12]]})
            if p == "/api/caches":
                os.makedirs(CACHE_DIR, exist_ok=True)
                names = sorted([f for f in os.listdir(CACHE_DIR) if f.endswith(".json")], reverse=True)[:20]
                if q.get("meta", ["0"])[0] in ("1", "true", "yes"):
                    return self._send({"ok": True, "caches": [cache_meta(n) for n in names]})
                return self._send({"ok": True, "caches": names})
            if p == "/api/replay":
                name = os.path.basename(q.get("file", [""])[0])
                fp = os.path.join(CACHE_DIR, name)
                if not os.path.isfile(fp):
                    return self._send({"ok": False, "error": "找不到缓存"}, 404)
                with open(fp, encoding="utf-8") as f:
                    return self._send({"ok": True, "data": json.load(f)})
            return self._send({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            return self._send({"ok": False, "error": "%s" % e, "trace": traceback.format_exc()[-800:]}, 500)

    def do_POST(self):
        touch()
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", "0") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            body = {}
        try:
            if u.path == "/api/scan":
                root = body.get("path") or HOME
                if not os.path.isdir(root):
                    return self._send({"ok": False, "error": "目录不存在: %s" % root}, 400)
                if not API_KEY:
                    return self._send({"ok": False, "error": "缺少环境变量 TYPESAFE_API_KEY"}, 400)
                # ★ 关键修复：新扫描先把还在跑的任务停掉。
                #   原来允许并发 —— 连点两次「开始扫描」就会有 2 个任务同时打 API，
                #   界面只跟最新那个，前面那个变成看不见的僵尸，整体看起来就是"卡住"。
                stale = running_jobs()
                for j in stale:
                    cancel_job(j, "被新的扫描顶掉")
                jid = uuid.uuid4().hex[:12]
                job = {"id": jid, "root": root, "state": "queued", "phase": "排队中",
                       "items": [], "judged": 0, "t0": time.time(),
                       "replaced": [j["id"] for j in stale]}
                JOBS[jid] = job
                prune_jobs()
                threading.Thread(target=run_job, args=(job, root), daemon=True).start()
                return self._send({"ok": True, "id": jid, "root": root,
                                   "cancelled": [j["id"] for j in stale]})
            if u.path == "/api/cancel":
                jid = body.get("id")
                if jid == "*" or not jid:                      # 不带 id = 取消全部
                    n = 0
                    for j in running_jobs():
                        cancel_job(j); n += 1
                    return self._send({"ok": True, "cancelled": n})
                j = JOBS.get(jid)
                if not j:
                    return self._send({"ok": False, "error": "job 不存在"}, 404)
                cancel_job(j)
                return self._send({"ok": True})
            if u.path == "/api/criteria":
                applied, errors = apply_criteria(body)
                if body.get("persist"):
                    try:
                        save_config()
                        applied.append("已写入 config.json（原文件备份为 config.json.bak）")
                    except Exception as e:
                        errors.append("持久化失败: %s" % e)
                return self._send({"ok": True, "applied": applied, "errors": errors,
                                   "criteria": CFG["criteria"], "gates": CFG["gates"],
                                   "rules": CFG["rules"]})
            if u.path == "/api/rejudge":
                if not API_KEY:
                    return self._send({"ok": False, "error": "缺少环境变量 TYPESAFE_API_KEY"}, 400)
                entries, root = None, None
                src = JOBS.get(body.get("id", ""))
                if src and src.get("entries"):
                    entries, root = src["entries"], src["root"]
                else:
                    cname = os.path.basename(body.get("cache", "") or "")
                    if cname:
                        fp = os.path.join(CACHE_DIR, cname)
                        if os.path.isfile(fp):
                            d = json.load(open(fp, encoding="utf-8"))
                            entries, root = d.get("entries"), d.get("root")
                if not entries:
                    return self._send({"ok": False, "error":
                        "没有可重判的条目 —— 请先扫描一次（新扫描会把原始条目留在内存和缓存里）"}, 400)
                nid = uuid.uuid4().hex[:12]
                job = {"id": nid, "root": root, "state": "queued", "phase": "排队中",
                       "items": [], "judged": 0, "t0": time.time(),
                       "entries": entries, "file_count": None,
                       "total_entries": len(entries), "rejudge_of": (src or {}).get("id")}
                JOBS[nid] = job
                threading.Thread(target=run_judge_only, args=(job,), daemon=True).start()
                return self._send({"ok": True, "id": nid, "rejudge_of": (src or {}).get("id")})
            if u.path == "/api/open":
                # 右键菜单「打开所在位置」：目录直接打开；文件在资源管理器里定位并选中。
                # 注意只用 explorer —— 不用 os.startfile 打开文件本身，那会执行它（exe 会跑起来）。
                pth = (body.get("path") or "").strip()
                if not pth or not os.path.exists(pth):
                    return self._send({"ok": False, "error": "路径不存在（可能已被移动或删除）"}, 400)
                try:
                    if os.path.isdir(pth):
                        os.startfile(pth)
                    else:
                        subprocess.Popen(["explorer", "/select,", pth])
                    return self._send({"ok": True})
                except Exception as e:
                    return self._send({"ok": False, "error": "%s" % e}, 500)
            if u.path == "/api/shutdown":
                self._send({"ok": True, "msg": "服务即将关闭"})

                def bye():
                    time.sleep(0.4)
                    print("\n[手动关闭] 服务已停止。")
                    os._exit(0)
                threading.Thread(target=bye, daemon=True).start()
                return
            return self._send({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            return self._send({"ok": False, "error": "%s" % e}, 500)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=SRV["port"])
    ap.add_argument("--host", default=SRV["host"])
    ap.add_argument("--idle-minutes", type=int, default=SRV["idle_shutdown_minutes"],
                    help="多久没人用就自动退出；给 0 表示不自动退出")
    ap.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    a = ap.parse_args()

    if not API_KEY:
        print("!! 没有找到环境变量 TYPESAFE_API_KEY —— 界面能开，但无法扫描判断")
    print("JEV 硬盘体检 MVP   http://%s:%d" % (a.host, a.port))
    print("模型 %s | 阈值 %sMB | 上限 %s 项 | 全程只读" % (
        MODEL, CFG["scan"]["min_size_mb"], CFG["scan"]["max_items"]))
    if a.idle_minutes > 0:
        print("无人操作 %d 分钟后会自动退出（关掉页面就会开始计时）" % a.idle_minutes)
    else:
        print("已关闭自动退出，需要手动关闭窗口")

    if not a.no_browser and SRV.get("open_browser"):
        def openlater():
            time.sleep(0.8)
            try:
                import webbrowser
                webbrowser.open("http://%s:%d" % (a.host, a.port))
            except Exception:
                pass
        threading.Thread(target=openlater, daemon=True).start()

    if a.idle_minutes > 0:
        SRV["idle_shutdown_minutes"] = a.idle_minutes
        threading.Thread(target=idle_watchdog, daemon=True).start()

    print("-" * 60)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
