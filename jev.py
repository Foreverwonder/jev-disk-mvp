#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jev.py — TypeSafe / Jev 最小可用客户端

用法:
    export TYPESAFE_API_KEY=apikey_xxx

    # 1) 命令行直接问（用 | 分隔：第一个是问题说明，后面是选项 / 档位）
    python jev.py judge \
        --state "滤芯装上去就漏水，我要退货退款！" \
        --choice "intent=用户想干什么|refund=退货退款|repair=维修|other=其他" \
        --noul   "urgent=是否表达紧迫感" \
        --score  "anger=用户有多生气|平静|不满但克制|非常愤怒"

    # 2) 从 JSON 文件读完整请求（复杂 state / 大批量问题用这个）
    python jev.py call payload.json

    # 3) 列出可用模型
    python jev.py models

设计取舍:
    - 模型默认锁 jev-1.13.0，不用 jev-latest 别名（别名会漂移，阈值会莫名失效）
    - 只依赖标准库，零安装
    - key 只从环境变量读，不落盘、不回显
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.typesafe.ai/v1/systemone"
MODELS_API = "https://api.typesafe.ai/v1/models"
DEFAULT_MODEL = "jev-1.13.0"


def _key():
    k = os.environ.get("TYPESAFE_API_KEY")
    if not k:
        sys.exit("缺少环境变量 TYPESAFE_API_KEY")
    return k


def _request(url, payload=None, timeout=90):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", "Bearer " + _key())
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit("HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")))
    except Exception as e:
        sys.exit("请求失败: %r" % (e,))


def judge(state, questions, model=DEFAULT_MODEL):
    """questions 形如:
    {
      "intent": {"type": "choice", "instructions": "...", "criteria": {"a": "...", "b": "..."}},
      "urgent": {"type": "noul",   "instructions": "..."},
      "anger":  {"type": "score",  "instructions": "...", "criteria": ["低", "中", "高"]},
    }
    """
    return _request(API, {"state": state, "model": model, "questions": questions})


def _split(spec):
    """'名=说明|选项1=描述1|选项2=描述2' -> ('名', '说明', ['选项1=描述1', ...])"""
    parts = spec.split("|")
    if "=" not in parts[0]:
        sys.exit("格式应为 名=说明|...，收到: %r" % spec)
    name, instr = parts[0].split("=", 1)
    return name.strip(), instr.strip(), parts[1:]


def _build_questions(a):
    q = {}
    for spec in a.choice or []:
        name, instr, opts = _split(spec)
        crit = {}
        for o in opts:
            if "=" in o:
                k, v = o.split("=", 1)
                crit[k.strip()] = v.strip()
            else:
                crit[o.strip()] = None
        if not crit:
            sys.exit("choice %r 至少要给一个选项" % name)
        q[name] = {"type": "choice", "instructions": instr, "criteria": crit}

    for spec in a.noul or []:
        name, instr, _ = _split(spec)
        q[name] = {"type": "noul", "instructions": instr}

    for spec in a.score or []:
        name, instr, levels = _split(spec)
        levels = [l.strip() for l in levels if l.strip()]
        if len(levels) < 2:
            sys.exit("score %r 建议至少 2 档（实测只给 1 档 API 也不报错，但那没意义）" % name)
        q[name] = {"type": "score", "instructions": instr, "criteria": levels}

    if not q:
        sys.exit("至少给一个问题：--choice / --noul / --score")
    return q


def _summarize(res):
    print("\n--- 摘要 ---", file=sys.stderr)
    for k, v in res.get("answers", {}).items():
        t = v["type"]
        if t == "choice":
            print("  %-14s choice=%s (conf %.2f)  %s" % (k, v["choice"], v["confidence"], v["probabilities"]), file=sys.stderr)
        elif t == "noul":
            print("  %-14s noul=%.2f" % (k, v["noul"]), file=sys.stderr)
        else:
            print("  %-14s score=%.2f (conf %.2f)  %s" % (k, v["score"], v["confidence"], v["probabilities"]), file=sys.stderr)
    print("  model=%s  usage=%s" % (res.get("model"), res.get("usage")), file=sys.stderr)


def cmd_judge(a):
    res = judge(a.state, _build_questions(a), a.model)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    _summarize(res)


def cmd_call(a):
    with open(a.payload, encoding="utf-8") as f:
        payload = json.load(f)
    payload.setdefault("model", DEFAULT_MODEL)
    res = _request(API, payload)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    _summarize(res)


def main():
    p = argparse.ArgumentParser(description="TypeSafe / Jev 最小客户端")
    p.add_argument("--model", default=DEFAULT_MODEL)
    sub = p.add_subparsers(dest="cmd", required=True)

    j = sub.add_parser("judge", help="一行命令问几个问题")
    j.add_argument("--state", required=True)
    j.add_argument("--choice", nargs="*", help='名=说明|选项=描述|选项=描述')
    j.add_argument("--noul", nargs="*", help='名=说明')
    j.add_argument("--score", nargs="*", help='名=说明|档位1|档位2|档位3')
    j.set_defaults(func=cmd_judge)

    c = sub.add_parser("call", help="从 JSON 文件读完整请求")
    c.add_argument("payload")
    c.set_defaults(func=cmd_call)

    m = sub.add_parser("models", help="列出可用模型")
    m.set_defaults(func=cmd_models)

    a = p.parse_args()
    a.func(a)


def cmd_models(a):
    print(json.dumps(_request(MODELS_API), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
