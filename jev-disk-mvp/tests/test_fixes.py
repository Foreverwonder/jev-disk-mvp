# -*- coding: utf-8 -*-
"""JEV 硬盘体检 · 缺陷回归测试

配套 2026-09-20 的评审实测：四个缺陷各自的断言。
规矩是「先红后绿」—— 改 server.py 之前这一套必须失败，改完必须全绿。

跑法（在 jev-disk-mvp 目录下）：
    python tests\\test_fixes.py
或：
    python -m unittest discover -s tests -t . -v

不依赖任何第三方库，跟项目一样只用标准库。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
if PKG not in sys.path:
    sys.path.insert(0, PKG)

import server  # noqa: E402


# ────────────────────────── 辅助 ──────────────────────────
def make_junction(link, target):
    """建 Windows 目录联接。普通用户权限即可，不需要管理员。"""
    if os.path.exists(link):
        return False
    # 不要 text=True：cmd 的输出是本地代码页，按 utf-8 解码会炸在后台线程里，污染测试输出。
    r = subprocess.run('cmd /c mklink /J "%s" "%s"' % (link, target),
                       capture_output=True)
    return r.returncode == 0 and os.path.isdir(link)


def rm_junction(link):
    """只摘链接本身，绝不碰目标内容。"""
    if not os.path.exists(link):
        return
    try:
        os.rmdir(link)
    except OSError:
        subprocess.run('cmd /c rmdir "%s"' % link, capture_output=True)


def mk_entry(i, size=None):
    """造一个「独立文件」条目。扩展名各异 → 每个自成等价类。"""
    size = size if size is not None else 1024 * (i + 1)
    nm = "thing%02d.a%d" % (i, i)
    path = "C:\\fake\\" + nm
    return {
        "path": path, "name": nm, "size": size, "files": 1,
        "mtime": "2025-01-01", "mtime_epoch": time.time() - 400 * 86400,
        "mtime_oldest": "2025-01-01", "mtime_oldest_epoch": time.time() - 400 * 86400,
        "idle_days_oldest": 400, "privacy": False, "is_dir": False,
        "sig": {"top_ext": [[".a%d" % i, 1, size]],
                "big_ext_by_count": [[".a%d" % i, 1]],
                "top_seg": [[nm, size]], "biggest": [[nm, size]]},
    }


def fake_ask_factory(fail_small_batch=False, drop_confidence=False):
    """伪装 JEV 接口。

    fail_small_batch：凡是「问题数 < 批次大小」的那一批直接抛异常
                      （用来确定性地打挂其中一批，不依赖并发顺序）。
    drop_confidence ：答案里不带 confidence 字段（模型漏字段的真实场景）。
    """
    def fake(questions, lines, stop=None):
        if fail_small_batch and len(questions) < server.CFG["batch"]["size"]:
            raise RuntimeError("HTTP 500: 模拟这一批挂了")
        answers = {}
        for k, v in questions.items():
            t = v.get("type")
            if t == "noul":
                answers[k] = {"noul": 0.95}
            elif t == "choice":
                a = {"choice": "regenerable_cache"}
                if not drop_confidence:
                    a["confidence"] = 0.8
                    a["probabilities"] = {"regenerable_cache": 0.8, "unknown": 0.2}
                answers[k] = a
            elif t == "score":
                a = {"score": 3.0}
                if not drop_confidence:
                    a["confidence"] = 0.8
                    a["probabilities"] = {"regenerable_cache": 0.8, "unknown": 0.2}
                answers[k] = a
        return {"answers": answers,
                "usage": {"input_tokens": 100, "output_tokens": 10}}
    return fake


# ────────────────── 缺陷 3：expand_dir 会走进 junction ──────────────────
@unittest.skipUnless(os.name == "nt", "junction 是 Windows 概念")
class ExpandDirJunctionTest(unittest.TestCase):
    """老实现用 os.walk + os.path.islink —— 两个对 junction 都失效，
    于是「点开看里面」会跟着联接重入，体积比这一行本身还大。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jev_exp_")
        self.real = os.path.join(self.tmp, "real_dir")
        os.makedirs(self.real)
        self.payload = 0
        for i in range(20):
            with open(os.path.join(self.real, "f%02d.bin" % i), "wb") as f:
                f.write(b"x" * 1024)
            self.payload += 1024
        self.link = os.path.join(self.tmp, "linked_dir")
        if not make_junction(self.link, self.real):
            self.skipTest("本机建不了 junction")

    def tearDown(self):
        rm_junction(self.link)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_junction_is_not_followed(self):
        r = server.expand_dir(self.tmp, 20000, 40)
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 20, "junction 里的文件被重复算进来了")
        self.assertEqual(r["total_size"], self.payload, "体积被 junction 灌水")

    def test_agrees_with_collect_files(self):
        """同一个目录，「点开看里面」必须和主扫描得到同一个数字。"""
        r = server.expand_dir(self.tmp, 20000, 40)
        files, _trunc, _abort, _st = server.collect_files(self.tmp)
        self.assertEqual(len(files), r["count"])
        self.assertEqual(sum(f[1] for f in files), r["total_size"])


# ────────── 缺陷 1 & 2：单批失败 / 缺字段，不能让整个任务作废 ──────────
class JudgeResilienceTest(unittest.TestCase):

    def setUp(self):
        self.entries = [mk_entry(i) for i in range(20)]   # 20 个类 → 拆成 15 + 5 两批

    def test_one_failed_batch_keeps_everything_else(self):
        """一批挂了，其它批的结论必须留下，挂掉的那些要露头而不是消失。"""
        job = {"usage": {}, "phase": ""}
        with mock.patch.object(server, "ask_jev", fake_ask_factory(fail_small_batch=True)):
            items, stats = server.judge_pipeline(job, self.entries)

        self.assertEqual(len(items), 20,
                         "有 %d 个条目凭空消失了（失败的那批被静默丢掉）" % (20 - len(items)))
        degraded = [it for it in items if it.get("degraded")]
        self.assertEqual(len(degraded), 5, "挂掉的那 5 个类没有标记出来")
        for it in degraded:
            self.assertEqual(it["kind"], "unknown")
            self.assertEqual(it["verdict"], "慎重")
            self.assertTrue(it["needs_human"])
        self.assertEqual(stats["degraded"], 5)

    def test_all_batches_failed_still_returns(self):
        """全挂也不能崩 —— 上层要能拿到「一个都没判出来」这个事实。"""
        job = {"usage": {}, "phase": ""}
        entries = [mk_entry(i) for i in range(4)]
        with mock.patch.object(server, "ask_jev",
                               side_effect=RuntimeError("HTTP 503: 服务不可用")):
            items, stats = server.judge_pipeline(job, entries)
        self.assertEqual(len(items), 4)
        self.assertEqual(stats["degraded"], 4)

    def test_missing_confidence_does_not_crash(self):
        """模型少回一个 confidence 字段，不能把整个任务带崩。"""
        job = {"usage": {}, "phase": ""}
        with mock.patch.object(server, "ask_jev", fake_ask_factory(drop_confidence=True)):
            items, stats = server.judge_pipeline(job, self.entries)

        self.assertEqual(len(items), 20)
        for it in items:
            self.assertEqual(it["row_conf"], 0.0, "没给把握就该按「零把握」处理")
            self.assertEqual(it["verdict"], "慎重")
            self.assertTrue(it["needs_human"])

    def test_missing_confidence_is_not_treated_as_confident(self):
        """反过来钉一下：不能因为字段缺失就默认成「有把握」。"""
        with mock.patch.object(server, "ask_jev", fake_ask_factory(drop_confidence=True)):
            items, _ = server.judge_pipeline({"usage": {}}, self.entries)
        self.assertFalse(any(it["verdict"] in ("建议删除", "可以考虑") for it in items))


# ────────── 缺陷 4：界面上的「只判了 N 次」不是真的请求次数 ──────────
class PhaseCountTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jev_job_")
        for i in range(20):
            with open(os.path.join(self.tmp, "blob%02d.a%d" % (i, i)), "wb") as f:
                f.write(b"y" * (1024 * (i + 1)))
        self.old_min = server.CFG["scan"]["min_size_mb"]
        self.old_max = server.CFG["scan"]["max_items"]
        self.old_cache = server.CACHE_DIR
        server.CFG["scan"]["min_size_mb"] = 0
        server.CFG["scan"]["max_items"] = 500
        server.CACHE_DIR = tempfile.mkdtemp(prefix="jev_cache_")

    def tearDown(self):
        server.CFG["scan"]["min_size_mb"] = self.old_min
        server.CFG["scan"]["max_items"] = self.old_max
        shutil.rmtree(server.CACHE_DIR, ignore_errors=True)
        server.CACHE_DIR = self.old_cache
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_phase_reports_real_request_count(self):
        job = {"root": self.tmp}
        with mock.patch.object(server, "ask_jev", fake_ask_factory()):
            server.run_job(job, self.tmp)

        self.assertEqual(job.get("state"), "done", job.get("error") or job.get("trace"))
        real = (job.get("usage") or {}).get("requests", 0)
        self.assertGreater(real, 0, "usage.requests 没被记上")

        m = re.search(r"只判了\s*([\d,]+)\s*次", job["phase"] or "")
        self.assertIsNotNone(m, "phase 里没有「只判了 N 次」：%r" % job["phase"])
        shown = int(m.group(1).replace(",", ""))
        self.assertEqual(shown, real,
                         "界面显示 %d 次，真实请求 %d 次：%r" % (shown, real, job["phase"]))

    def test_usage_is_always_present(self):
        """一个请求都没发成时，前端也不该因为拿不到 usage 而空着。"""
        job = {"root": self.tmp}
        with mock.patch.object(server, "ask_jev",
                               side_effect=RuntimeError("HTTP 503")):
            server.run_job(job, self.tmp)
        self.assertIsNotNone(job.get("usage"), "全挂时 job 里没有 usage")


if __name__ == "__main__":
    unittest.main(verbosity=2)
