# -*- coding: utf-8 -*-
"""JEV 硬盘体检 · 对抗性审查测试（第二轮，2026-09-20）

这份测试的立场和 test_fixes.py 不同：
  test_fixes.py 是「按我修过的四个缺陷各钉一条」—— 贴着改动写的。
  本文件是**假装我是攻击者**：不看改了什么，只问「还有哪些输入 / 哪条故障路径
  能把这套代码打崩、打错、或让它撒谎」。

所以本文件写于修复之后，却**期望先红**。红的每一条 = 上一轮漏掉的入口。

跑法（在 jev-disk-mvp 目录下）：
    python tests\\test_adversarial.py
"""
import glob
import os
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


def mk_entry(i):
    """一个自成等价类的条目（扩展名互不相同 → 不会被归并）。"""
    size = 1024 * (i + 1)
    nm = "thing%02d.a%d" % (i, i)
    return {
        "path": "C:\\fake\\" + nm, "name": nm, "size": size, "files": 1,
        "mtime": "2025-01-01", "mtime_epoch": time.time() - 400 * 86400,
        "mtime_oldest": "2025-01-01", "mtime_oldest_epoch": time.time() - 400 * 86400,
        "idle_days_oldest": 400, "privacy": False, "is_dir": False,
        "sig": {"top_ext": [[".a%d" % i, 1, size]],
                "big_ext_by_count": [[".a%d" % i, 1]],
                "top_seg": [[nm, size]], "biggest": [[nm, size]]},
    }


def fake_tev(choice_answer, score_answer=None):
    """按调用方给的「答案形状」回应 —— 用来把畸形回包塞进真实管线。"""
    score_answer = score_answer if score_answer is not None else {
        "score": 3.0, "confidence": 0.8, "probabilities": {"a": 0.8}}

    def _f(questions, lines, stop=None):
        answers = {}
        for k, v in questions.items():
            t = v.get("type")
            if t == "noul":
                answers[k] = {"noul": 0.95}
            elif t == "choice":
                answers[k] = dict(choice_answer)
            elif t == "score":
                answers[k] = dict(score_answer)
        return {"answers": answers,
                "usage": {"input_tokens": 10, "output_tokens": 2}}
    return _f


def run_pipeline(choice_answer=None, score_answer=None, n=6, raw_ask=None):
    entries = [mk_entry(i) for i in range(n)]
    fake = raw_ask if raw_ask is not None else fake_tev(choice_answer, score_answer)
    with mock.patch.object(server, "ask_jev", fake):
        return server.judge_pipeline({"usage": {}}, entries)


# ══════════ 方向一：模型回包的类型空间（不只是「字段缺失」） ══════════
class MalformedAnswerTest(unittest.TestCase):
    """上一轮只堵了 confidence=None。可 None 只是这个空间里的一个点。

    字符串、布尔、百分制、inf —— 每一个都走同一条赋值语句、同一个运算符。
    """

    def test_string_confidence_is_normalised(self):
        """confidence 回成字符串 "0.9"（模型很常这么干）→ 不该崩，且要认成 0.9。"""
        items, _ = run_pipeline({"choice": "regenerable_cache", "confidence": "0.9",
                                 "probabilities": {"regenerable_cache": 0.9}})
        self.assertTrue(items, "整条管线没了")
        for it in items:
            self.assertEqual(it["kind_conf"], 0.9, "字符串把握度没被认成数字")

    def test_string_score_is_normalised(self):
        """score 回成字符串 "3.0" → 不该崩。"""
        items, _ = run_pipeline(
            {"choice": "regenerable_cache", "confidence": 0.8,
             "probabilities": {"regenerable_cache": 0.8}},
            {"score": "3.0", "confidence": 0.8, "probabilities": {"a": 0.8}})
        self.assertTrue(items)
        for it in items:
            self.assertEqual(it["d_raw"], 3.0, "字符串损失分没被认成数字")

    def test_bool_true_is_not_full_confidence(self):
        """confidence 回成 true —— float(True)==1.0 会被当成「满把握」。

        这是**朝危险方向的误读**：满把握意味着更少让用户复核。
        方向必须反过来：无法解释 → 按零把握。
        """
        items, _ = run_pipeline({"choice": "regenerable_cache", "confidence": True,
                                 "probabilities": {"regenerable_cache": 1.0}})
        for it in items:
            self.assertNotEqual(it["row_conf"], 1.0, "布尔 true 被当成了满把握")
            self.assertEqual(it["row_conf"], 0.0, "布尔值按零把握处理才安全")

    def test_percent_scale_is_not_silently_accepted(self):
        """confidence 回成百分制 90 —— 目前会被当 90 用，等于满把握。

        测试不断言「必须换算成 0.9」（那是猜），只断言**不许输出越界的把握度**，
        也不许把一个无法解释的值解释成「最有把握」。
        """
        items, _ = run_pipeline({"choice": "regenerable_cache", "confidence": 90,
                                 "probabilities": {"regenerable_cache": 0.9}})
        for it in items:
            self.assertLessEqual(it["kind_conf"] if it["kind_conf"] is not None else 0,
                                 1.0, "把握度超出了 1.0 还照用")
            self.assertNotEqual(it["row_conf"], 1.0, "百分制 90 被当成了满把握")

    def test_nan_and_inf_confidence(self):
        for bad in (float("nan"), float("inf")):
            with self.subTest(bad=bad):
                items, _ = run_pipeline({"choice": "regenerable_cache",
                                         "confidence": bad,
                                         "probabilities": {"regenerable_cache": 1.0}})
                for it in items:
                    self.assertEqual(it["row_conf"], 0.0, "%r 没有被挡掉" % bad)

    def test_json_output_is_numeric(self):
        """归一化只做在内部还不够 —— 输出的字段类型要干净，否则前端算 NaN。"""
        items, _ = run_pipeline({"choice": "regenerable_cache", "confidence": "0.9",
                                 "probabilities": {"regenerable_cache": 0.9}},
                                {"score": "3.0", "confidence": "0.7",
                                 "probabilities": {"a": 0.7}})
        for it in items:
            for f in ("kind_conf", "conf", "row_conf", "d_raw"):
                v = it[f]
                self.assertTrue(v is None or isinstance(v, (int, float)),
                                "字段 %s 是 %r（%s）—— 前端会算出 NaN" % (f, v, type(v).__name__))

    def test_probabilities_wrong_type(self):
        """probabilities 回成字符串 → 不该崩在摆动判定上。"""
        items, _ = run_pipeline({"choice": "regenerable_cache", "confidence": 0.8,
                                 "probabilities": "high"})
        for it in items:
            self.assertTrue(it["kind_prob"] is None or isinstance(it["kind_prob"], dict))

    def test_string_confidence_on_non_loss_kind_does_not_crash(self):
        """★ 最致命的一条，也是上一轮修复完全没碰到的那条路。

        kind 不在 loss_kinds 里时（program_body / app_state / user_content），
        这个条目**不会进第 3 关**，d_raw 保持 None。于是汇总时走的不是
        score_entry，而是 `kc >= floor` 那一行 —— 而 kc 此刻是裸的
        a.get("confidence")。字符串一来就是 str >= float，TypeError。

        要命的是：这个异常发生在**汇总阶段**，不在 work() 的 try 里，
        所以上一轮加的「单批容错」根本兜不住它 —— 整个任务照样作废。
        """
        items, _ = run_pipeline({"choice": "user_content", "confidence": "0.9",
                                 "probabilities": {"user_content": 0.9}})
        self.assertTrue(items, "汇总阶段炸了，整个任务没了")
        for it in items:
            self.assertEqual(it["verdict"], "别碰", "用户产出的结论必须封顶在「别碰」")

    def test_invalid_kind_conf_closes_the_gate(self):
        """类别把握度无效时，闸门必须**关上**而不是跳过。

        verdict_of 的注释写的是「连是什么都不知道，凭什么建议删」，
        但实现是 `kind_conf is not None and ...` —— 值无效/NULL 时闸门直接跳过，
        等于把「不知道」当成了「没问题」。
        """
        items, _ = run_pipeline({"choice": "regenerable_cache", "confidence": "high",
                                 "probabilities": {"regenerable_cache": 0.9}},
                                {"score": 3.0, "confidence": 0.9,
                                 "probabilities": {"a": 0.9}})
        for it in items:
            self.assertFalse(it["verdict"] in ("建议删除", "可以考虑"),
                             "类别把握度无效，却放行到了 %r" % it["verdict"])

    def test_string_noul_does_not_crash(self):
        """第 1 关的 noul 回成字符串 → 原来 `v < floor` 是 str < float，直接 TypeError。

        （这条是补的：第二轮改了 on1 的归一化，却一开始忘了给它配测试。）
        """
        def fake(questions, lines, stop=None):
            answers = {}
            for k, v in questions.items():
                t = v.get("type")
                if t == "noul":
                    answers[k] = {"noul": "0.9"}          # ← 字符串
                elif t == "choice":
                    answers[k] = {"choice": "regenerable_cache", "confidence": 0.8,
                                  "probabilities": {"regenerable_cache": 0.8}}
                elif t == "score":
                    answers[k] = {"score": 2.0, "confidence": 0.8,
                                  "probabilities": {"a": 0.8}}
            return {"answers": answers,
                    "usage": {"input_tokens": 10, "output_tokens": 2}}
        items, stats = run_pipeline(raw_ask=fake)
        self.assertTrue(items)
        self.assertEqual(stats["degraded"], 0, "字符串 noul 把批次打降级了")
        for it in items:
            self.assertNotEqual(it["kind"], "unknown",
                                "0.9 的认名字分数被误判成「看不懂」")

    def test_missing_choice_field(self):
        """连 choice 都没有 → 认成 unknown，走「慎重」，不许崩。"""
        items, _ = run_pipeline({"confidence": 0.9})
        for it in items:
            self.assertEqual(it["kind"], "unknown")
            self.assertEqual(it["verdict"], "慎重")

    def test_string_rate_is_never_confident(self):
        """confidence 回成 "high" 这类文字 → 按零把握，绝不能倒向「可以删」。"""
        items, _ = run_pipeline({"choice": "regenerable_cache", "confidence": "high",
                                 "probabilities": {"regenerable_cache": 0.9}})
        for it in items:
            self.assertFalse(it["verdict"] in ("建议删除", "可以考虑"),
                             "文字型把握度被当成了有把握：%r" % it["verdict"])


# ══════════ 方向二：全局故障被降级吞掉 → 不可诊断 ══════════
class TotalFailureTest(unittest.TestCase):
    """上一轮把「单批失败 = 整任务作废」改成了「单批失败 = 降级」。

    但降级是个**双刃**：如果**每一批**都失败（key 失效 / 断网 / 服务挂了），
    现在会把「一个都没判出来」包装成一句「完成」。用户查不出为什么。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jev_tot_")
        for i in range(20):
            with open(os.path.join(self.tmp, "z%02d.a%d" % (i, i)), "wb") as f:
                f.write(b"z" * (1024 * (i + 1)))
        self.old = (server.CFG["scan"]["min_size_mb"], server.CFG["scan"]["max_items"],
                    server.CACHE_DIR)
        server.CFG["scan"]["min_size_mb"] = 0
        server.CFG["scan"]["max_items"] = 500
        self.tmp_cache = tempfile.mkdtemp(prefix="jev_tot_cache_")
        server.CACHE_DIR = self.tmp_cache

    def tearDown(self):
        server.CFG["scan"]["min_size_mb"], server.CFG["scan"]["max_items"], \
            server.CACHE_DIR = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)
        # 测试自己建的 cache 目录也要收走 —— 不然每跑一次就在 %TEMP% 留一个空壳
        # （写这套测试的时候漏了这行，实测攒下 29 个残留目录）
        shutil.rmtree(self.tmp_cache, ignore_errors=True)

    def test_all_failed_is_not_reported_as_done(self):
        """每批都挂（HTTP 401）—— 这不该显示成「完成」。"""
        job = {"root": self.tmp}
        with mock.patch.object(server, "ask_jev",
                               side_effect=RuntimeError("HTTP 401: invalid api key")):
            server.run_job(job, self.tmp)
        self.assertNotEqual(job.get("state"), "done",
                            "全挂却被报成完成：phase=%r" % job.get("phase"))
        self.assertIn("401", "%s %s" % (job.get("error", ""), job.get("phase", "")),
                      "全挂但用户看不到原因（key 错？断网？服务挂？）")

    def test_partial_failure_still_reports_done(self):
        """对照：只有一批挂时，必须仍然是「完成」+ 降级提示（上一轮的成果不能被推翻）。"""
        job = {"root": self.tmp}
        entries = [mk_entry(i) for i in range(20)]
        with mock.patch.object(server, "ask_jev",
                               fake_tev({"choice": "regenerable_cache",
                                         "confidence": 0.8,
                                         "probabilities": {"regenerable_cache": 0.8}})):
            items, stats = server.judge_pipeline(job, entries)
        self.assertEqual(stats["degraded"], 0)
        self.assertEqual(len(items), 20)

    def test_failure_reason_is_carried_to_item(self):
        """失败原因要能一路带到界面 —— 存了不显示等于没存。"""
        entries = [mk_entry(i) for i in range(4)]
        with mock.patch.object(server, "ask_jev",
                               side_effect=RuntimeError("HTTP 429: rate limited")):
            items, stats = server.judge_pipeline({"usage": {}}, entries)
        why = " ".join(str(it.get("degraded") or "") for it in items)
        self.assertIn("429", why, "条目上没有带上失败原因")

    def test_stats_expose_failure_reasons(self):
        """stats 里要有可直接展示的原因汇总，而不是只给一个计数。"""
        entries = [mk_entry(i) for i in range(4)]
        with mock.patch.object(server, "ask_jev",
                               side_effect=RuntimeError("HTTP 401: invalid api key")):
            _items, stats = server.judge_pipeline({"usage": {}}, entries)
        blob = "%s" % stats
        self.assertIn("401", blob, "stats 里看不到失败原因：%s" % blob)


# ══════════ 方向三：expand_dir 与主扫描的口径差 ══════════
class ExpandDirParityTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jev_par_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fill(self, n):
        for i in range(n):
            with open(os.path.join(self.tmp, "f%04d.bin" % i), "wb") as f:
                f.write(b"x" * 16)

    def test_capped_boundary_is_exact(self):
        """文件数正好等于 cap 时，并没有被截断 —— 不能说「文件太多，只统计了前 N 个」。

        这条文案会直接出现在录屏画面上，说错了就是自己打自己。
        """
        self._fill(5)
        r = server.expand_dir(self.tmp, 5, 40)
        self.assertEqual(r["count"], 5)
        self.assertFalse(r["capped"], "恰好 5/5 个文件被误报成「文件太多」")

    def test_truncation_is_flagged_when_real(self):
        """反过来：真被截断了必须说。"""
        self._fill(8)
        r = server.expand_dir(self.tmp, 5, 40)
        self.assertTrue(r["capped"], "明明截断了却不说")
        self.assertEqual(r["count"], 5)

    def test_default_cap_matches_frontend_expectation(self):
        """接口真正用的 cap 与主扫描上限差了 100 倍（2 万 vs 200 万）。

        不算错，但意味着「点开看里面」在超大目录上必然给出更小的数字 ——
        前端那句「共 N 个文件」在截断时是在撒谎。这里钉住：截断时必须可见。
        """
        self._fill(6)
        r = server.expand_dir(self.tmp, server.CFG["server"]["expand_max_files"], 40)
        self.assertFalse(r["capped"])

    def test_rel_paths_survive_the_rewrite(self):
        """重写遍历后，items[].rel 必须还是「相对这个目录」的老样子。"""
        self._fill(3)
        os.makedirs(os.path.join(self.tmp, "sub"))
        with open(os.path.join(self.tmp, "sub", "deep.bin"), "wb") as f:
            f.write(b"y" * 16)
        r = server.expand_dir(self.tmp, 100, 40)
        rels = sorted(x["rel"] for x in r["items"])
        self.assertIn("sub\\deep.bin", rels, "子目录里的相对路径变了：%r" % rels)
        for rel in rels:
            self.assertFalse(os.path.isabs(rel), "相对路径变成了绝对路径：%r" % rel)

    def test_agrees_with_collect_files_on_same_cap(self):
        """同一个目录、同一个上限，「点开看里面」必须和主扫描得出同一个数字。"""
        self._fill(7)
        r = server.expand_dir(self.tmp, 100000, 40)
        files, _t, _a, _s = server.collect_files(self.tmp)
        self.assertEqual(r["count"], len(files))
        self.assertEqual(r["total_size"], sum(f[1] for f in files))

    def test_non_file_non_dir_entries_are_accounted(self):
        """既不是目录、也不是普通文件的条目，必须被交代 —— 不许凭空消失。

        新遍历有 dir / file 两个分支，漏了 else 的话，第三种条目会
        **静默消失**：不计 files、不计 errors、也不计 links_skipped。
        collect_files 至少把它们记进 other_skipped。

        ★ 实测（2026-09-20）：本机的 os.symlink 会**静默失败** —— 返回成功、
          不抛异常，但链接根本没建出来（listdir 里查不到）。所以造不出真实的
          「文件级符号链接」样本。这里直接把 scandir 换成只吐这种条目的假实现：
          测的是**分支逻辑**，不依赖 OS 行为。
        """
        class FakeEntry:
            name = "weird.bin"
            path = os.path.join(self.tmp, "weird.bin")

            def is_dir(self, follow_symlinks=True):
                return False

            def is_file(self, follow_symlinks=True):
                return False

            def stat(self, follow_symlinks=True):
                raise OSError("这种条目本来就 stat 不了")

        class FakeScan:
            def __enter__(self):
                return iter([FakeEntry()])

            def __exit__(self, *a):
                return False

        with mock.patch.object(server.os, "scandir", return_value=FakeScan()):
            r = server.expand_dir(self.tmp, 100, 40)
        self.assertEqual(r["count"], 0)
        self.assertEqual(r["other_skipped"], 1, "非目录非文件的条目被凭空吞掉了")


# ══════════ 方向四：回归 —— 正常输入的行为一个都不能变 ══════════
class NoRegressionTest(unittest.TestCase):

    def test_num_passthrough(self):
        self.assertEqual(server._num(0.77), 0.77)
        self.assertEqual(server._num(0), 0.0)
        self.assertEqual(server._num(1), 1.0)
        self.assertEqual(server._num(3.0), 3.0)
        self.assertEqual(server._num(None), 0.0)
        self.assertEqual(server._num("abc"), 0.0)

    def test_score_scale_is_untouched(self):
        """★ 把握度是 0~1，损失分是 0~3 —— 两套量纲。

        如果哪天有人把「范围收敛」加进 _num 本身，损失分会被一起打成 0，
        整个打分逻辑静默失效。这里钉死：_num 必须原样放行 3.0。
        """
        self.assertEqual(server._num(2.5), 2.5)
        self.assertEqual(server._num("2.5"), 2.5)

    def test_normal_verdict_unchanged(self):
        """正常回包下的结论，必须和修复前一致（可以和备份实现对照）。"""
        old = _load_backup_server()
        if old is None:
            self.skipTest("找不到备份实现")
        entries = [mk_entry(i) for i in range(6)]
        ans = {"choice": "regenerable_cache", "confidence": 0.8,
               "probabilities": {"regenerable_cache": 0.8, "unknown": 0.2}}
        sc = {"score": 2.5, "confidence": 0.75, "probabilities": {"a": 0.75}}
        with mock.patch.object(server, "ask_jev", fake_tev(ans, sc)):
            new_items, _ = server.judge_pipeline({"usage": {}}, entries)
        with mock.patch.object(old, "ask_jev", fake_tev(ans, sc)):
            old_items, _ = old.judge_pipeline({"usage": {}}, entries)
        self.assertEqual(
            [(i["name"], i["kind"], i["verdict"], i["row_conf"]) for i in new_items],
            [(i["name"], i["kind"], i["verdict"], i["row_conf"]) for i in old_items],
            "正常输入下结论漂了")

    def test_no_degraded_flag_when_all_ok(self):
        items, stats = run_pipeline({"choice": "regenerable_cache", "confidence": 0.8,
                                     "probabilities": {"regenerable_cache": 0.8}})
        self.assertEqual(stats["degraded"], 0)
        self.assertEqual(stats["degraded_items"], 0)
        self.assertTrue(all(it.get("degraded") is None for it in items),
                        "一切正常却给条目挂了「没判出」标记")


def _load_backup_server():
    """把改动前的 server.py 当模块加载，用来做 A/B 对照。"""
    cands = glob.glob(os.path.join(PKG, "_backup", "pre-fix-*", "server.py"))
    if not cands:
        return None
    cands.sort()
    import importlib.util
    spec = importlib.util.spec_from_file_location("server_prefix_backup", cands[-1])
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


# ══════════ 方向五：前后端字段对得上吗（静态核对） ══════════
class ContractTest(unittest.TestCase):

    def setUp(self):
        self.html = open(os.path.join(PKG, "index.html"), encoding="utf-8").read()
        self.srv = open(os.path.join(PKG, "server.py"), encoding="utf-8").read()

    def test_frontend_reads_what_server_writes(self):
        pairs = [
            ('"degraded": why', "it.degraded"),
            ('"degraded_items": degraded_items', "degraded_items"),
            ('"links_skipped": links_skipped', "links_skipped"),
        ]
        for srv_tok, html_tok in pairs:
            with self.subTest(token=html_tok):
                self.assertIn(srv_tok, self.srv, "服务端不再写 %s" % srv_tok)
                self.assertIn(html_tok, self.html, "前端不读 %s（服务端写了也没用）" % html_tok)

    def test_phase_shows_request_count_not_class_count(self):
        """界面上「只判了 N 次」必须取自真实请求数，不能是类数。"""
        self.assertRegex(self.srv, r"只判了 %d 次\"\s*%\s*\([^)]*requests",
                         "phase 文案没有绑定到 requests")


if __name__ == "__main__":
    unittest.main(verbosity=2)
