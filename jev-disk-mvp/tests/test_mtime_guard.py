# -*- coding: utf-8 -*-
"""JEV 硬盘体检 · 坏时间戳回归测试（2026-09-20 第三轮）

事故现场：
    扫 C:\\ 到「正在归纳条目…」时整场崩掉 ——
        OSError: [Errno 22] Invalid argument
        server.py line 475  entry_dir -> time.strftime("%Y-%m-%d", time.localtime(oldest))

根因（实测，不是猜的）：
    C:\\Users\\<user>\\AppData\\LocalLow\\Tencent\\WeType\\Dict\\**\\*.bin
    有 5 个微信输入法词典文件的 mtime = -11644318675.68624（约公元 1601 年）。
    Windows 的 CRT 不接受负时间戳 —— 实测 time.localtime(-1) 就报
    OSError: [Errno 22] Invalid argument。gmtime 也一样。
    全盘 135.8 万个文件里就这 5 个，而它足以让 112 万文件的扫描白跑。

不只是"会崩"这么简单 —— 坏时间戳还会**污染**：
    build_entries 的 lo = min(所有文件 mtime)，混进一个负数，整个目录的
    "最老内容"就变成 1601 年，年代视图会把微信输入法目录归进 1969 年前。
    就算不崩，结论也是错的。

跑法（在 jev-disk-mvp 目录下）：
    python tests\\test_mtime_guard.py
"""
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
if PKG not in sys.path:
    sys.path.insert(0, PKG)

import server  # noqa: E402


# ────────────────────────── 素材 ──────────────────────────
# 真实元凶的原始值，别改成 -1 这种整数值 —— 小数位是现场特征
BAD_REAL = -11644318675.68624          # 微信输入法词典 .bin
GOOD = 1700000000.0                    # 2023-11-15 前后，正常时间戳

WEIXIN_DICT = [
    r"C:\Users\<user>\AppData\LocalLow\Tencent\WeType\Dict\bwcjp\1169\bwcjpmac_jianpin.bin",
    r"C:\Users\<user>\AppData\LocalLow\Tencent\WeType\Dict\bwcn\1346\bwcnmac.bin",
    r"C:\Users\<user>\AppData\LocalLow\Tencent\WeType\Dict\bwcnmacv8\39\bwcnmacv8.bin",
    r"C:\Users\<user>\AppData\LocalLow\Tencent\WeType\Dict\ckvd\456\ckvdmac.bin",
    r"C:\Users\<user>\AppData\LocalLow\Tencent\WeType\Dict\slwd\1306\slwd_pc_second_level.bin",
]


class TestFmtDate(unittest.TestCase):
    """时间格式化：任何输入都不许抛异常。"""

    def test_real_culprit_value(self):
        """现场抓到的那个值 —— 必须不抛，降级成「未知」。"""
        self.assertEqual(server.fmt_date(BAD_REAL), server.FMT_UNKNOWN)

    def test_negative_epoch(self):
        for v in (-1, -86400, -2208988800, -11644473600):
            with self.subTest(v=v):
                self.assertEqual(server.fmt_date(v), server.FMT_UNKNOWN)

    def test_nan_and_inf(self):
        for v in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(v=v):
                self.assertEqual(server.fmt_date(v), server.FMT_UNKNOWN)

    def test_out_of_range(self):
        for v in (1e12, 253402300799, 32536799999):
            with self.subTest(v=v):
                self.assertEqual(server.fmt_date(v), server.FMT_UNKNOWN)

    def test_junk_types(self):
        for v in (None, "abc", [], {}, object()):
            with self.subTest(v=v):
                self.assertEqual(server.fmt_date(v), server.FMT_UNKNOWN)

    def test_valid_values_untouched(self):
        """正常时间戳不能被误伤成「未知」—— 边界开太宽就是这个后果。"""
        for v in (0, 1, GOOD, time.time()):
            with self.subTest(v=v):
                got = server.fmt_date(v)
                self.assertNotEqual(got, server.FMT_UNKNOWN)
                self.assertRegex(got, r"^\d{4}-\d{2}-\d{2}$")

    def test_matches_localtime_for_good_values(self):
        self.assertEqual(
            server.fmt_date(GOOD),
            time.strftime("%Y-%m-%d", time.localtime(GOOD)))


class TestSafeEpoch(unittest.TestCase):

    def test_bad_is_none(self):
        for v in (BAD_REAL, -1, float("nan"), float("inf"), 1e12, None, "x"):
            with self.subTest(v=v):
                self.assertIsNone(server.safe_epoch(v))

    def test_good_passthrough(self):
        self.assertEqual(server.safe_epoch(GOOD), GOOD)
        self.assertEqual(server.safe_epoch(0), 0.0)

    def test_accepts_int_and_numeric_string(self):
        self.assertEqual(server.safe_epoch(1700000000), 1700000000.0)
        self.assertEqual(server.safe_epoch("1700000000"), 1700000000.0)


class TestIdleDays(unittest.TestCase):

    def test_bad_mtime_not_a_absurd_number(self):
        """−11644318675 会让「闲置天数」算出 13 万天 —— 不能就这么displayed。"""
        self.assertIsNone(server.idle_days(BAD_REAL))

    def test_normal_value_still_works(self):
        self.assertAlmostEqual(
            server.idle_days(time.time() - 10 * 86400), 10.0, delta=0.05)

    def test_future_mtime_clamps_to_zero(self):
        self.assertEqual(server.idle_days(time.time() + 86400 * 30), 0.0)


class TestBuildEntriesSurvives(unittest.TestCase):
    """端到端：坏 mtime 混进扫描结果，整场归纳不能崩。"""

    ROOT = "C:\\__jev_mtime_fixture__"

    def _files(self, bad_at_root=True, bad_in_dir=True):
        root = self.ROOT
        out = []
        if bad_at_root:
            # root 直接下的一个超大文件 → 走 entry_file
            out.append((root + "\\huge_bad.bin", 200 * 1024 * 1024, BAD_REAL))
        # 一个 15 个文件的子目录（> collapse_min_files=12）→ 走 entry_dir
        sub = root + "\\Dict"
        for i in range(14):
            out.append((sub + "\\f%02d.bin" % i, 2 * 1024 * 1024, GOOD))
        if bad_in_dir:
            out.append((sub + "\\poison.bin", 2 * 1024 * 1024, BAD_REAL))
        return out

    def test_does_not_raise(self):
        entries, total = server.build_entries(self.ROOT, self._files())
        self.assertTrue(entries)

    def test_dir_entry_mtime_is_unknown_not_crash(self):
        entries, _ = server.build_entries(self.ROOT, self._files())
        d = [e for e in entries if e["path"].endswith("Dict")]
        self.assertTrue(d, "Dict 目录应当折叠成一条")
        self.assertRegex(d[0]["mtime"], r"^(\d{4}-\d{2}-\d{2}|%s)$" % server.FMT_UNKNOWN)

    def test_oldest_not_polluted_by_bad_value(self):
        """核心断言：目录的「最老内容」不能被 1601 年的坏值拉走。"""
        entries, _ = server.build_entries(self.ROOT, self._files())
        d = [e for e in entries if e["path"].endswith("Dict")][0]
        self.assertEqual(
            d["mtime_oldest"],
            time.strftime("%Y-%m-%d", time.localtime(GOOD)),
            "最老内容应当取正常文件的时间，而不是被负数污染")

    def test_sig_oldest_epoch_is_sane(self):
        entries, _ = server.build_entries(self.ROOT, self._files())
        d = [e for e in entries if e["path"].endswith("Dict")][0]
        oldest = d["sig"]["mtime_oldest"]
        self.assertIsNotNone(oldest)
        self.assertGreater(oldest, 0, "epoch 不该是负数")
        self.assertLess(oldest, 4e9, "epoch 不该是 2100 年以后")

    def test_all_bad_dir_still_produces_entry(self):
        """整个目录全是坏时间戳 → 不能因为「算不出时间」就把条目丢了。"""
        files = [(self.ROOT + "\\Dict\\f%02d.bin" % i, 2 * 1024 * 1024, BAD_REAL)
                 for i in range(14)]
        entries, _ = server.build_entries(self.ROOT, files)
        self.assertTrue(entries)

    def test_entries_are_json_safe(self):
        """NaN / Infinity 不是合法 JSON —— 浏览器 JSON.parse 会直接白屏。"""
        entries, _ = server.build_entries(self.ROOT, self._files())
        s = json.dumps(entries, ensure_ascii=False)
        self.assertNotIn("NaN", s)
        self.assertNotIn("Infinity", s)
        json.loads(s)   # 能原样读回来

    def test_real_bad_mtime_variants(self):
        """不止现场那一个值 —— 各种坏时间戳都得扛住。"""
        bads = [BAD_REAL, -1.0, float("nan"), float("inf"), 1e12, 253402300799.0]
        for b in bads:
            with self.subTest(bad=b):
                files = [(self.ROOT + "\\Dict\\f%02d.bin" % i, 2 * 1024 * 1024, b)
                         for i in range(14)]
                entries, _ = server.build_entries(self.ROOT, files)
                self.assertTrue(entries)


class TestRealFilesOnThisMachine(unittest.TestCase):
    """现场取证：真去 stat 那 5 个文件，确认它们仍然会炸（修好之前）。"""

    def test_real_files_have_bad_mtime(self):
        found = 0
        for p in WEIXIN_DICT:
            if not os.path.exists(p):
                continue
            m = os.stat(p).st_mtime
            found += 1
            with self.subTest(p=p):
                self.assertLess(m, 0, "现场取证：这些文件确实是负时间戳")
        if not found:
            self.skipTest("微信输入法词典文件已不在本机（换机器/已卸载）")

    def test_real_files_do_not_crash_formatter(self):
        for p in WEIXIN_DICT:
            if not os.path.exists(p):
                continue
            with self.subTest(p=p):
                server.fmt_date(os.stat(p).st_mtime)   # 不抛即通过


class TestCollectFilesPath(unittest.TestCase):
    """另一条链路：collect_files 的 items[].mtime（line 132 那个格式化点）。"""

    def test_items_mtime_safe_with_real_bad_timestamp(self):
        tmp = tempfile.mkdtemp(prefix="jev_mtime_")
        try:
            p = os.path.join(tmp, "bad.bin")
            with open(p, "wb") as f:
                f.write(b"x" * 32)
            try:
                os.utime(p, (BAD_REAL, BAD_REAL))
            except (OSError, OverflowError, ValueError):
                self.skipTest("这台机器的 os.utime 不接受负时间戳，无法构造现场")
            got = os.stat(p).st_mtime
            if got >= 0:
                self.skipTest("负时间戳没写进去，无法构造现场")

            files, truncated, aborted, stats = server.collect_files(
                tmp, None, lambda: False)
            self.assertTrue(files)
            # 只要不抛异常就算过 —— 修好之前这里会 OSError
            server.fmt_date(files[0][2])
        finally:
            try:
                os.remove(os.path.join(tmp, "bad.bin"))
            except OSError:
                pass
            try:
                os.rmdir(tmp)
            except OSError:
                pass


class TestDownstreamConsumers(unittest.TestCase):
    """坏时间戳流到下游的那两道关。

    「idle_days 对坏值返回 None」之后，这两处原本是 round(浮点) 和浮点除法 ——
    会直接 TypeError。是这次修复自己带出来的回归风险，必须锁住。
    """

    def _entry(self, epoch, is_dir=False):
        return {"path": "C:\\x\\y", "name": "y", "size": 1000, "files": 1,
                "mtime": server.FMT_UNKNOWN, "mtime_epoch": epoch,
                "mtime_oldest": server.FMT_UNKNOWN, "mtime_oldest_epoch": epoch,
                "idle_days_oldest": None, "privacy": False, "sig": {},
                "is_dir": is_dir}

    def test_score_entry_unknown_time_is_neutral(self):
        d, sh, ag = server.score_entry(self._entry(None), "cache", 0.9, 2.0)
        self.assertIsInstance(d, float)
        self.assertEqual(ag, 1.0, "时间未知既不该有年龄加成，也不该被惩罚")

    def test_score_entry_old_thing_still_boosted(self):
        """别把正常路径一起废掉 —— 400 天没动应当拿到加成。"""
        old = time.time() - 400 * 86400
        _, _, ag = server.score_entry(self._entry(old), "cache", 0.9, 2.0)
        self.assertGreater(ag, 1.0)

    def test_score_entry_recent_thing_no_boost(self):
        _, _, ag = server.score_entry(self._entry(time.time()), "cache", 0.9, 2.0)
        self.assertAlmostEqual(ag, 1.0, places=2)


class TestDegradedFallback(unittest.TestCase):
    """单条构造炸了 → 降级保留。不许丢条目，不许二次抛。"""

    ROOT = "C:\\__jev_degrade_fixture__"

    def _files(self):
        sub = self.ROOT + "\\Dict"
        fs = [(sub + "\\f%02d.bin" % i, 2 * 1024 * 1024, GOOD) for i in range(14)]
        fs.append((self.ROOT + "\\big.bin", 200 * 1024 * 1024, GOOD))
        return fs

    def test_entry_construction_failure_keeps_entry(self):
        from unittest import mock

        def boom(_):
            raise RuntimeError("boom")

        with mock.patch.object(server, "privacy_hit", side_effect=boom):
            entries, _ = server.build_entries(self.ROOT, self._files())
        self.assertTrue(entries, "单条构造炸了也必须留下条目，不能让整场白跑")
        self.assertTrue(all(e["size"] > 0 for e in entries), "体积不能凭空丢")
        self.assertTrue(any(e["mtime"] == server.FMT_UNKNOWN
                            for e in entries), "降级条目应当标「未知」")


class TestRealScenarioEndToEnd(unittest.TestCase):
    """真刀真枪：拿现场那个目录（微信输入法词典）跑一遍完整归纳。

    修好之前，这里就是用户看到的那次崩溃。
    """

    BASE = r"C:\Users\<user>\AppData\LocalLow\Tencent\WeType"

    def test_collect_and_build_on_real_dir(self):
        if not os.path.isdir(self.BASE):
            self.skipTest("本机没有微信输入法目录")
        files, truncated, aborted, stats = server.collect_files(
            self.BASE, None, lambda: False)
        if not files:
            self.skipTest("目录读不到文件")
        bad = [f for f in files if f[2] < 0]
        self.assertTrue(bad, "这个目录里本该有负时间戳文件（现场证据）")

        entries, _ = server.build_entries(self.BASE, files)   # ← 修好前必炸
        self.assertTrue(entries)
        s = json.dumps(entries, ensure_ascii=False)
        self.assertNotIn("NaN", s)
        self.assertNotIn("Infinity", s)

    def test_real_dir_oldest_not_1601(self):
        if not os.path.isdir(self.BASE):
            self.skipTest("本机没有微信输入法目录")
        files, _, _, _ = server.collect_files(self.BASE, None, lambda: False)
        if not files:
            self.skipTest("目录读不到文件")
        entries, _ = server.build_entries(self.BASE, files)
        for e in entries:
            with self.subTest(path=e["path"]):
                self.assertNotIn(e["mtime_oldest"], ("1601-01-01", "1969-12-31"),
                                 "坏值不该被当成真实日期显示出来")


if __name__ == "__main__":
    unittest.main(verbosity=2)
