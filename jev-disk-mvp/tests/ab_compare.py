# -*- coding: utf-8 -*-
"""A/B 对照工具：同一份回包，喂给「改动前」和「当前」两版代码，逐条比结果。

为什么要有这个文件：
    test_*.py 只回答「现在对不对」。修完之后真正要回答的是
    「修复前到底会怎样」—— 没有这个对照，「修了个真问题」就只是我的一面之词。

用法（在 jev-disk-mvp 目录下）：
    python tests\\ab_compare.py                      # 默认拿最新的 round2-baseline-*
    python tests\\ab_compare.py pre-fix-              # 指定基线目录前缀

不调真实 API：ask_jev 一律用假的。
"""
import glob
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

PREFIX = sys.argv[1] if len(sys.argv) > 1 else "round2-baseline-"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mk_entries(n=6):
    out = []
    for i in range(n):
        size = 1024 * (i + 1)
        nm = "thing%02d.a%d" % (i, i)
        out.append({
            "path": "C:\\fake\\" + nm, "name": nm, "size": size, "files": 1,
            "mtime": "2025-01-01", "mtime_epoch": time.time() - 400 * 86400,
            "mtime_oldest": "2025-01-01",
            "mtime_oldest_epoch": time.time() - 400 * 86400,
            "idle_days_oldest": 400, "privacy": False, "is_dir": False,
            "sig": {"top_ext": [[".a%d" % i, 1, size]],
                    "big_ext_by_count": [[".a%d" % i, 1]],
                    "top_seg": [[nm, size]], "biggest": [[nm, size]]},
        })
    return out


def fake(noul=0.95, choice="regenerable_cache", conf=0.8, score=2.0):
    def _f(questions, lines, stop=None):
        answers = {}
        for k, v in questions.items():
            t = v.get("type")
            if t == "noul":
                answers[k] = {"noul": noul}
            elif t == "choice":
                a = {"choice": choice, "probabilities": {"regenerable_cache": 0.8}}
                if conf is not False:
                    a["confidence"] = conf
                answers[k] = a
            elif t == "score":
                answers[k] = {"score": score, "confidence": 0.8,
                              "probabilities": {"a": 0.8}}
        return {"answers": answers, "usage": {"input_tokens": 5, "output_tokens": 1}}
    return _f


def probe(mod, label, **kw):
    """跑一次，返回「结果摘要」而不是异常 —— 崩溃本身就是要看的信息。"""
    try:
        with mock.patch.object(mod, "ask_jev", fake(**kw)):
            items, stats = mod.judge_pipeline({"usage": {}}, mk_entries())
        it = items[0] if items else {}
        return {"ok": True, "n": len(items), "degraded": stats.get("degraded"),
                "kind_conf": it.get("kind_conf"), "row_conf": it.get("row_conf"),
                "verdict": it.get("verdict"), "kind": it.get("kind"),
                "json": _json_legality(items)}
    except Exception as e:
        return {"ok": False, "err": "%s: %s" % (type(e).__name__, e)}


def _json_legality(payload):
    """模拟浏览器 JSON.parse：Infinity / NaN 都算非法（会让界面直接白屏）。"""
    def boom(x):
        raise ValueError("非法常量 %s" % x)
    try:
        json.loads(json.dumps(payload), parse_constant=boom)
        return "合法"
    except Exception:
        return "★非法"


CASES = [
    ("正常回包", dict()),
    ("第1关 noul 回字符串 '0.9'", dict(noul="0.9")),
    ("第2关 confidence 回字符串 '0.9' + 非 loss 类", dict(choice="user_content", conf="0.9")),
    ("confidence 回布尔 true", dict(conf=True)),
    ("confidence 回 inf", dict(conf=float("inf"))),
    ("confidence 回百分制 90", dict(conf=90)),
    ("confidence 回文字 'high'", dict(conf="high")),
    ("只缺 kind 的 confidence", dict(conf=False)),
]


def main():
    cands = sorted(glob.glob(os.path.join(PKG, "_backup", PREFIX + "*", "server.py")))
    out = []
    if not cands:
        out.append("找不到基线（_backup/%s*）—— 只能报当前行为。" % PREFIX)
        base = None
    else:
        base = load(cands[-1], "srv_ab_base")
        out.append("基线：%s" % os.path.relpath(cands[-1], PKG))
    cur = load(os.path.join(PKG, "server.py"), "srv_ab_cur")
    out.append("对照：server.py（当前）")
    out.append("")

    for name, kw in CASES:
        b = probe(base, "base", **kw) if base else None
        c = probe(cur, "cur", **kw)

        def fmt(r):
            if r is None:
                return "（无基线）"
            if not r["ok"]:
                return "崩：%s" % r["err"][:70]
            return ("items=%d deg=%s kc=%-6r row=%-6r %-6s json=%s"
                    % (r["n"], r["degraded"], r["kind_conf"], r["row_conf"],
                       r["verdict"], r["json"]))

        out.append("【%s】" % name)
        out.append("   基线 -> %s" % fmt(b))
        out.append("   现在 -> %s" % fmt(c))
        out.append("")

    # 全批失败时任务层怎么报
    out.append("【全部批次失败（HTTP 401）时任务层怎么报】")
    for tag, m in (("基线", base), ("现在", cur)):
        if m is None:
            out.append("   %s -> （无基线）" % tag)
            continue
        tmp = tempfile.mkdtemp(prefix="jev_ab_")
        for i in range(20):
            with open(os.path.join(tmp, "z%02d.a%d" % (i, i)), "wb") as f:
                f.write(b"z" * (1024 * (i + 1)))
        o_min, o_max, o_cache = (m.CFG["scan"]["min_size_mb"],
                                 m.CFG["scan"]["max_items"], m.CACHE_DIR)
        m.CFG["scan"]["min_size_mb"] = 0
        m.CFG["scan"]["max_items"] = 500
        m.CACHE_DIR = tempfile.mkdtemp(prefix="jev_ab_cache_")
        try:
            job = {"root": tmp}
            with mock.patch.object(m, "ask_jev",
                                   side_effect=RuntimeError("HTTP 401: invalid api key")):
                m.run_job(job, tmp)
            out.append("   %s -> state=%-6s phase=%r" % (tag, job.get("state"), job.get("phase")))
            out.append("            error=%r" % (job.get("error") or ""))
        finally:
            m.CFG["scan"]["min_size_mb"], m.CFG["scan"]["max_items"], \
                m.CACHE_DIR = o_min, o_max, o_cache
            shutil.rmtree(tmp, ignore_errors=True)

    text = "\n".join(out)
    dest = os.path.join(PKG, "tests", "ab_compare_last.txt")
    with open(dest, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print("\n[已保存] %s" % os.path.relpath(dest, PKG))


if __name__ == "__main__":
    main()
