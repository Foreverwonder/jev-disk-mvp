# TypeSafe / Jev 实战笔记

> 2026-09-20 实测。下面每个数字都是拿真 key 打出来的，不是抄文档。文档抄来的部分我会标「文档」。

## 一句话

Jev 不是"又一个聊天模型"。它不写字、不给你看推理过程，只回答你出的**选择题** —— 返回一个选择 + 一串概率。
定位是"给代码用的常识模块"，不是给人看的助手。官方原话：*LLM produces words for people, Jev produces typed decisions.*

---

## 一、装了什么

| 项 | 值 |
| --- | --- |
| 落盘位置 | `~/.workbuddy/skills/typesafe-ai/` |
| 文件 | `SKILL.md` 10040 字节 + `LICENSE` 1068 字节（与 GitHub 报的字节数**完全一致**） |
| 来源 | github.com/typesafe-ai/skills（public / MIT / 342 star / 2026-08-24 建） |
| 安全审计 | ✅ P2 干净 —— 目录里只有 SKILL.md 和 LICENSE，**无脚本、无 hook、无二进制** |
| 安装方式 | 官方安装器 `npx skills add` 的 agent 名单里**没有 WorkBuddy**（有 codebuddy / openclaw / hermes-agent），所以走了官方也认可的手动安装：把 `skills/typesafe-ai` 整个目录拷进 agent 的 skills 目录 |

官方明确提醒：**只用一种方式装，别重复装**，会留多份副本。

---

## 二、基础用法

**端点**（文档 + 实测）

```
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

**请求三件套**：`state`（要判断的材料，字符串 / JSON 对象 / 数组）、`model`、`questions`（你给每个问题起个 key，答案用同样的 key 回来）。

**三个原语**（你定义答案空间，模型只负责选）：

| 想干嘛 | type | 返回什么 |
| --- | --- | --- |
| 从一堆选项里选一个 | `choice` | `choice` + `probabilities`（各选项概率，和为 1）+ `confidence` |
| 是 / 否判断 | `noul` | `noul`（0~1 的概率）**没有 confidence** |
| 沿着一个维度打分 | `score` | `score`（可以落在两档之间）+ `legend` + `probabilities` + `confidence` |

**实测请求 / 响应**（真实工单分类）

```json
// 请求
{
  "state": "客服你好，我上个月买的那台净水器，滤芯装上去就一直漏水……要求退货退款，不然我就去投诉12315了。",
  "model": "jev-latest",
  "questions": {
    "urgency": {"type": "noul", "instructions": "这条信息是否表达了紧迫性？"},
    "intent":  {"type": "choice", "instructions": "用户最主要想做什么？",
                "criteria": {"refund": "要求退货退款", "repair": "要求上门维修",
                             "compensate": "要求赔偿损失", "ask_info": "只是咨询信息"}},
    "anger":   {"type": "score", "instructions": "用户有多生气？",
                "criteria": ["平静陈述", "不满但克制", "非常愤怒，威胁投诉"]}
  }
}
```

```json
// 响应（HTTP 200，服务端耗时 118ms，用掉 544 input tokens）
{
  "model": "jev-1.13.0",
  "answers": {
    "urgency": {"type": "noul", "noul": 0.84},
    "intent":  {"type": "choice", "choice": "refund", "confidence": 1.0,
                "probabilities": {"refund": 1.0, "repair": 0.0, "compensate": 0.0, "ask_info": 0.0}},
    "anger":   {"type": "score", "score": 2.0, "confidence": 1.0,
                "legend": {"0": "平静陈述", "1": "不满但克制", "2": "非常愤怒，威胁投诉"},
                "probabilities": {"0": 0.0, "1": 0.0, "2": 1.0}}
  },
  "usage": {"input_tokens": 544, "output_tokens": 79}
}
```

**⭐ 最值钱的一条特性：一次请求能塞任意多个问题，它们在服务端并行跑。**
实测一次塞 6 个问题（3 种原语混用），服务端耗时 128ms —— 跟只问 1 个几乎没差别，token 才 490。
这意味着"扇出"成本近乎为零，你可以放心多问。

**列模型**：`GET /v1/models` → 返回 `jev-latest` 和 `jev-preview`（当前都指向 `jev-1.13.0`）。

---

## 三、额度与成本

### 限流（文档，且明确标注「动态调整，可能不打招呼就变」）

- **250,000 tokens / 秒**
- **1,200 请求 / 分钟**

超了返回 `429`；官方 SDK 默认带指数退避重试，直接调 HTTP 就得自己处理。

### 上下文

- 单次请求 **64k tokens** 上限
- 其中 `state` + 最长那个问题 ≤ **32k**
- **只吃文本**。图片 / 音频 / 视频要先自己转成文字或结构化字段

### 价格

- **$42 / 十亿 input tokens = $0.042 / 百万 input tokens**
- **输出不计费**

实测换算：一次 544 tokens 的调用 ≈ **$0.0000228**；跑一万次约 **$0.23**（≈1.6 元）。

### ⚠️ 账号额度查不到

- `/v1/usage`、`/v1/me`、`/v1/account`、`/v1/keys`、`/v1/billing` → **全是 404**（API 面只有 `/v1/systemone`、`/v1/models`、`/health`）
- 响应头里**不带** any rate-limit 字段（我检查过所有响应头）
- `typesafe.ai/pricing` → **404**，官网没有公开定价页

**结论**：余额 / 已用量 / 套餐只能登 <https://console.typesafe.ai> 看。想在程序里预判还剩多少额度，做不到 —— 只能靠 429 反馈。

---

## 四、跟普通 LLM 到底差在哪

| 维度 | 普通 LLM | Jev / System One |
| --- | --- | --- |
| 产出 | 一段文字 | 类型化判断 + 概率 |
| 要不要解析 | 要，还得防它不听话 | 不用，schema 已由你定死 |
| 不确定性 | 通常没有，或自己瞎编 | 每次给 `confidence` + 完整分布 |
| 速度 | 秒级 | **实测服务端 59~146 ms** |
| 价格 | 贵 | $0.042/Mtok，输出免费 |
| 一问多答 | 一次一个回合 | **一问多答，服务端并行** |
| 幻觉 | 有 | 厂商宣称"零幻觉"（因为它不生成文本） |
| 写文案 / 写代码 / 讲理由 | 擅长 | **不会，也不该让它干** |

厂商自己的口径：同等任务 **193.6x 更快、444.6x 更便宜**（对照例子：Jev $0.000081 / 0.114s vs LLM $0.013880 / 8.566s）。这是他们挑的任务，别全信；但**量级跟我实测的速度对得上**。

### 实测补充的三条（文档没写）

**1. 它并不完全确定 —— 别把阈值卡在边界上。**
同一个请求连跑 3 次：`billing` 这个**离散选择没变**，但概率在抖 —— 0.68 / 0.65 / 0.69，confidence 在 0.47 / 0.47 / 0.53 之间跳。
→ 拿 confidence 卡阈值，**留 0.05 左右余量**。

**2. 中文能用，但要知道它的定位。**
中文工单分类、3847 字中文长文（4209 tokens，耗时 97ms）都判对了；中文当选项 key 也能**原样返回、不乱码**。
但文档明说**英文才是主场**，非英文场景必须自己验证 + 盯紧 confidence。

**3. 模糊输入它还是会偏。**
给一句"嗯，就这样吧。"问"是否明确同意"，实测给 **0.61~0.63**，而不是理想的 0.5。
→ 可真可假的场景，光看概率不够，得配合阈值一起判。

---

## 五、踩到的坑

1. **score 只给 1 档不报错**。文档写"至少 2 个 level"，实测传 1 档返回 **200**，直接给 `score: 0.0`。校验比文档松，别指望它帮你拦错。
2. **别名会漂移**。`jev-latest` 现在指向 `jev-1.13.0`，以后会自动变。**生产环境锁版本号**（写 `jev-1.13.0`），否则某天阈值会莫名失效。
3. **没有 rate-limit 响应头**，观察不到剩余额度，只能靠 429。
4. **限流是动态的**，可能随时变。
5. **首次调用有 TLS 握手开销**（实测 ~6.7s），之后稳定在 0.6~1.2s（含跨境网络往返）。
6. **choice 缺 criteria** → `422`，报错精确到字段路径（`body.questions.x.choice.criteria`），这点做得不错；**模型名写错** → `400 Unknown model: gpt-4o`。

---

## 六、什么时候用它

**合适**
- 分类 / 路由（工单派给谁、意图识别）
- 打分排序（相关性、质量分、优先级）
- 校验（这条引用站得住吗、这个字段值对不对、这封邮件是不是钓鱼）
- 任何"需要一点常识、但要可复现 / 可组合 / 便宜"的判断

**不合适**
- 要写文案、写代码、要给人解释理由 → 还是回去用 LLM
- 需要看图 / 听音频 / 看视频（目前只吃文本）
- 需要"想清楚再答"的复杂多步推理

**推荐搭配**：Jev 当"快速判断层"，拿不准的（confidence 低）再升级给 LLM 或人 —— 这就是官方说的 "verify and escalate"/"confidence-gated routing"。

---

## 七、最小可用代码

```python
import json, os, urllib.request

def judge(state, questions, model="jev-1.13.0"):
    """model 锁死版本号，别用别名，否则阈值会随版本漂移失效。"""
    req = urllib.request.Request(
        "https://api.typesafe.ai/v1/systemone",
        data=json.dumps({"state": state, "model": model, "questions": questions},
                        ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Authorization": "Bearer " + os.environ["TYPESAFE_API_KEY"],
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


out = judge("滤芯装上去就漏水，打三天电话没人接，我要求退货退款！", {
    "intent": {"type": "choice", "instructions": "用户最主要想做什么？",
               "criteria": {"refund": "退货退款", "repair": "维修", "compensate": "赔偿", "other": "其他"}},
    "urgent": {"type": "noul", "instructions": "是否表达了紧迫感？"},
    "anger":  {"type": "score", "instructions": "用户有多生气？",
               "criteria": ["平静", "不满但克制", "非常愤怒"]},
})

a = out["answers"]
print(a["intent"]["choice"], a["intent"]["confidence"])   # refund 1.0
print(a["urgent"]["noul"])                                # 0.84
print(a["anger"]["score"], a["anger"]["legend"])           # 2.0 {...}

# 按风险分层决策（官方推荐姿势）
if a["intent"]["confidence"] < 0.5:
    route_to_human()          # 它自己说没把握，别猜
elif a["intent"]["choice"] == "refund":
    confirm_then_execute()    # 高影响动作，先确认
```

**要点**
- key 放环境变量 / 服务端，**别写进前端**
- 所有问题 + 阈值常量**集中在一个文件**里，方便人工 review（官方建议，也在理）
- 拿不准就用 `probabilities` 自己算，不必非用它的 `confidence`

---

## 八、官方资源

- 文档索引（喂给 agent 最好用）：<https://docs.typesafe.ai/llms.txt>
- Playground（网页里直接试）：<https://console.typesafe.ai/playground>
- 拿 key / 看用量：<https://console.typesafe.ai/keys>
- Cookbook 一堆现成配方：<https://docs.typesafe.ai/llms.txt> 里搜 `cookbooks/`
- 已知缺陷说明：<https://docs.typesafe.ai/model-jaggedness/jev-1.13>

**更新 skill**：`npx skills update`，或直接用 GitHub 最新版覆盖整个 skill 目录。
