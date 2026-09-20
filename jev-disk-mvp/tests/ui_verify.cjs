/*
 * 可用性修复 · 端到端核验（真实 Edge + 真实扫描）
 * ---------------------------------------------------------------
 * 用真实浏览器把 JEV 硬盘体检点一遍，并对七项修复逐条断言。
 * 关键点：不靠读代码猜，靠「屏幕上显示的数字 vs 缓存文件里的真值」对账。
 *
 * 用法：node tests/ui_verify.cjs <扫描目录> [输出目录]
 *   默认扫描目录：E:\whisper_models（约 3 秒 / 4 次调用 / 成本约 $0.0003）
 * 前提：8849 端口的服务已带 TYPESAFE_API_KEY 起好。
 */
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9224;
const PAGE = 'http://127.0.0.1:8849/';
const API = 'http://127.0.0.1:8849';
const SCAN_DIR = process.argv[2] || 'E:\\whisper_models';
const OUT = process.argv[3] || path.join(__dirname);
const sleep = ms => new Promise(r => setTimeout(r, ms));

const L = [];
let pass = 0, fail = 0;
const fails = [];
const say = s => L.push(s);
function A(name, cond, detail) {
  if (cond) { pass++; say(`  ok   ${name}`); }
  else { fail++; fails.push(name); say(`  FAIL ${name}${detail ? '  <<< ' + detail : ''}`); }
}
function jget(u) { return fetch(API + u).then(r => r.json()); }

async function waitVersion() {
  for (let i = 0; i < 120; i++) {
    try { const r = await fetch(`http://127.0.0.1:${PORT}/json/version`); if (r.ok) return await r.json(); } catch (e) {}
    await sleep(250);
  }
  throw new Error('CDP 调试端口没起来');
}
class CDP {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map();
    ws.addEventListener('message', ev => {
      const m = JSON.parse(ev.data);
      if (m.id && this.pending.has(m.id)) {
        const p = this.pending.get(m.id); this.pending.delete(m.id);
        m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result);
      }
    });
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    return new Promise((res, rej) => {
      this.pending.set(id, { res, rej });
      this.ws.send(JSON.stringify(Object.assign({ id, method, params }, sessionId ? { sessionId } : {})));
    });
  }
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const profile = path.join(process.env.TEMP, 'uf_edge_profile_' + Date.now());
  const child = spawn(EDGE, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--remote-debugging-port=' + PORT, '--user-data-dir=' + profile,
    '--remote-allow-origins=*', '--hide-scrollbars', '--window-size=1440,1000',
    'about:blank'
  ], { stdio: 'ignore' });

  let sessionId = null, ws = null;
  try {
    const ver = await waitVersion();
    ws = new WebSocket(ver.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
    const cdp = new CDP(ws);
    const t = await cdp.send('Target.createTarget', { url: PAGE });
    sessionId = (await cdp.send('Target.attachToTarget', { targetId: t.targetId, flatten: true })).sessionId;
    await cdp.send('Page.enable', {}, sessionId);
    await cdp.send('Runtime.enable', {}, sessionId);
    await cdp.send('Emulation.setDeviceMetricsOverride',
      { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false }, sessionId);

    const ev = async expr => {
      const r = await cdp.send('Runtime.evaluate',
        { expression: expr, awaitPromise: true, returnByValue: true, userGesture: true }, sessionId);
      if (r.exceptionDetails) throw new Error('页面 JS 抛错: ' + JSON.stringify(r.exceptionDetails).slice(0, 400));
      return r.result.value;
    };

    /* ---------- 等页面起来 ---------- */
    for (let i = 0; i < 80; i++) {
      if (await ev("document.readyState==='complete' && !!document.querySelector('#rows')")) break;
      await sleep(250);
    }
    await sleep(2000);
    await ev("document.querySelector('#picker').classList.remove('show')");
    say('== JEV 硬盘体检 · 可用性修复核验 ==');
    say('扫描目录：' + SCAN_DIR);
    say('');

    /* =========================================================
     * ① 真跑一次扫描（P0-1 数字一致 / P0-2 归并类上屏）
     * ========================================================= */
    const before = (await jget('/api/caches')).caches || [];
    say('[扫描] 真实跑一次 —— ' + SCAN_DIR);
    await ev(`PICKED=${JSON.stringify(SCAN_DIR)}; document.querySelector('#curPath').textContent=PICKED; startScan(PICKED); 0`);
    let done = false;
    for (let i = 0; i < 300; i++) {                      // 最多等 150 秒
      done = await ev("document.querySelector('#btnScan').textContent.includes('重新扫描')");
      if (done) break;
      await sleep(500);
    }
    A('扫描能跑完（没卡住 / 没崩）', done);
    await sleep(1200);

    const foot2 = await ev("document.querySelector('#foot2').textContent");
    const foldCalc = await ev("document.querySelector('#foldCalc').textContent");
    const headURL = await ev("document.querySelector('#shown').textContent");

    // 拿缓存真值对账
    const after = (await jget('/api/caches')).caches || [];
    const fresh = after.filter(x => !before.includes(x))[0] || after[0];
    const truth = (await jget('/api/replay?file=' + encodeURIComponent(fresh))).data;
    const reqT = (truth.usage || {}).requests, tokT = (truth.usage || {}).input_tokens;
    const clsT = (truth.dedup || {}).classes;

    say('');
    say(`  屏幕 · 列表脚注 : ${foot2}`);
    say(`  屏幕 · 概要行   : ${foldCalc}`);
    say(`  缓存 · 真值     : ${reqT} 次 / ${tokT} token / 归并 ${clsT} 类 (${fresh})`);
    say('');

    A('脚注与缓存真值：调用次数一致', String(reqT) && foot2.includes(String(reqT) + ' 次调用'),
      `脚注没有出现真值 ${reqT} 次`);
    A('脚注与缓存真值：token 一致',
      String(tokT) && foot2.includes((tokT / 1000).toFixed(1) + 'k'),
      `脚注没有出现真值 ${(tokT / 1000).toFixed(1)}k`);
    A('概要行显示出「归并 N 类」（P0-2）', /归并\s*\d+\s*类/.test(foldCalc),
      `概要行=${foldCalc}`);
    A('概要行的类数与缓存真值一致', foldCalc.includes('归并 ' + clsT + ' 类'),
      `期望 归并 ${clsT} 类，实际 ${foldCalc}`);
    A('概要行首尾完整（文件数 → 归并 → 判 N 条）',
      /\d+ 个文件.*归并 \d+ 类.*判 \d+ 条/.test(foldCalc), `概要行=${foldCalc}`);
    A('清单头部有「N 项」', /\d+ 项/.test(headURL), headURL);

    /* =========================================================
     * ② 按落点（P0-3）：不能是一根 100% 的柱子
     * ========================================================= */
    say('[② 构成] 按落点是否分得开');
    await ev("document.querySelectorAll('.tabs button[data-v]')[1].click()");
    await sleep(700);
    const loc = await ev(`(()=>{
      const box=document.querySelector('#byLoc');
      const rows=[...box.querySelectorAll('.big')];
      return { n: rows.length, labels: rows.map(r=>r.querySelector('span').textContent.trim()) };
    })()`);
    const insight = await ev("document.querySelector('#insight').textContent");
    say(`  落点柱数 = ${loc.n}  ${JSON.stringify(loc.labels)}`);
    say(`  一句话洞察 = ${insight}`);
    A('按落点分出多格（不是单柱）', loc.n > 1, `只有 ${loc.n} 格：${JSON.stringify(loc.labels)}`);
    A('落点标签不是盘符（D:/E: 这种）',
      !loc.labels.some(x => /^[A-Za-z]:$/.test(x)), JSON.stringify(loc.labels));
    A('落点标签是真实子目录名',
      loc.labels.some(x => x.includes('whisper') || x.includes('models') || x.includes('（根目录')), JSON.stringify(loc.labels));
    A('一句话洞察不是「堆在 D:/E:」这种废话', !/堆在\s*[A-Za-z]:\s*$/.test(insight.trim()), insight);

    // 顺带确认按类别的柱子还在（没被改坏）
    const kind = await ev("document.querySelectorAll('#byKind .big').length");
    A('按类别那一栏没被改坏（仍有柱子）', kind > 0, `kind 柱数=${kind}`);

    /* =========================================================
     * ③ 清单出口：搜索 / 筛选 / 折叠（P1-1）
     * ========================================================= */
    say('');
    say('[① 清单] 有没有出口（搜索 / 筛选 / 折叠）');
    await ev("document.querySelectorAll('.tabs button[data-v]')[0].click()");
    await sleep(500);
    const tools = await ev(`(()=>({
      q: !!document.querySelector('#q'),
      chips: document.querySelectorAll('#fltChips [data-act]').length,
      grpClickable: [...document.querySelectorAll('#rows .grp')].every(g=>g.hasAttribute('data-grp')),
      grpCount: document.querySelectorAll('#rows .grp').length,
      cntEl: !!document.querySelector('#listCnt')
    }))()`);
    say(`  搜索框=${tools.q} 筛选项=${tools.chips} 分组条可点=${tools.grpClickable}(${tools.grpCount}) 计数=${tools.cntEl}`);
    A('有搜索框', tools.q);
    A('有动作筛选', tools.chips >= 3, `chips=${tools.chips}`);
    A('分组条可点击（能折叠）', tools.grpClickable && tools.grpCount > 0, JSON.stringify(tools));
    A('有「当前显示 N 项」计数', tools.cntEl);

    if (tools.q) {
      // 搜一个不存在的词 → 应该有明确空态，而不是一片空白
      const n0 = await ev("document.querySelectorAll('#rows .tr').length");
      await ev(`(()=>{const q=document.querySelector('#q');q.value='zzz_nothing_zzz';
        q.dispatchEvent(new Event('input',{bubbles:true}));return 0})()`);
      await sleep(400);
      const n1 = await ev("document.querySelectorAll('#rows .tr').length");
      const empty = await ev("(document.querySelector('#rows').textContent||'').includes('没有')||(document.querySelector('#rows').textContent||'').includes('没匹配')");
      await ev(`(()=>{const q=document.querySelector('#q');q.value='';
        q.dispatchEvent(new Event('input',{bubbles:true}));return 0})()`);
      await sleep(400);
      const n2 = await ev("document.querySelectorAll('#rows .tr').length");
      say(`  搜索前 ${n0} 行 → 搜不存在的词 ${n1} 行 → 清空 ${n2} 行`);
      A('搜索真的会收窄列表', n1 < n0, `${n0} → ${n1}`);
      A('搜不到时有空态提示（不是白屏）', empty);
      A('清空搜索后恢复', n2 === n0, `${n2} vs ${n0}`);
    }

    if (tools.grpClickable && tools.grpCount > 0) {
      // 锁定同一个组 —— 不能每次取「第一个」，否则折叠后组序一变，测的就不是同一件事
      const key = await ev("document.querySelector('#rows .grp[data-grp]').dataset.grp");
      const sel = `#rows .grp[data-grp="${key}"]`;
      const before1 = await ev("document.querySelectorAll('#rows .tr').length");

      await ev(`document.querySelector('${sel}').click()`);
      await sleep(400);
      const collapsed = await ev("document.querySelectorAll('#rows .tr').length");
      const headStill = await ev(`!!document.querySelector('${sel}')`);
      const headTxt = await ev(`(document.querySelector('${sel}')||{textContent:''}).textContent`);
      const markedFold = await ev(`(document.querySelector('${sel}')||{classList:{contains:()=>false}}).classList.contains('folded')`);

      await ev(`document.querySelector('${sel}').click()`);
      await sleep(400);
      const restored = await ev("document.querySelectorAll('#rows .tr').length");

      say(`  组「${key}」：折叠前 ${before1} 行 → 折叠后 ${collapsed} 行 → 再点 ${restored} 行`);
      say(`  折叠后组头还在=${headStill}  文字=${headTxt.replace(/\s+/g, ' ').trim()}`);
      A('点分组条能收起（行数变少）', collapsed < before1, `${before1} → ${collapsed}`);
      A('收起后分组条**仍然在**（否则再也点不开）', headStill, '组头消失了');
      A('收起后组头标出「已收起」', /已收起/.test(headTxt), headTxt);
      A('收起后组头有 folded 标记', markedFold);
      A('再点能展开回原样', restored === before1, `${restored} vs ${before1}`);
    }

    /* =========================================================
     * ④ 排序口径（P1-2）：文案不能说大话
     * ========================================================= */
    say('');
    say('[① 清单] 排序口径是否说清楚');
    const thTip = await ev("document.querySelector('.thc[data-k=\"conf\"]').getAttribute('title') || ''");
    const grpToggle = await ev("!!document.querySelector('#grpToggle')");
    say(`  把握列 tooltip = ${thTip}`);
    say(`  分组开关存在 = ${grpToggle}`);
    A('把握列 tooltip 说明了「组内排序」', /组内/.test(thTip), thTip);
    A('有关闭分组的开关（想看全局排序）', grpToggle);

    if (grpToggle) {
      await ev("document.querySelector('#rows .thc, #tbl .thc[data-k=\"conf\"]').click()");
      await sleep(200);
      await ev("document.querySelector('#grpToggle').click()");
      await sleep(500);
      const confs = await ev("[...document.querySelectorAll('#rows .tr .conf')].slice(0,12).map(e=>parseFloat(e.textContent))");
      const sortedAsc = confs.every((v, i) => i === 0 || confs[i - 1] <= v + 1e-9);
      say(`  关掉分组后前 12 行把握 = ${JSON.stringify(confs)}`);
      A('关掉分组后把握是真的全局升序（小的浮上来）', sortedAsc, JSON.stringify(confs));
      await ev("document.querySelector('#grpToggle').click()");
      await sleep(400);
    }

    /* =========================================================
     * ⑤ 脱敏（P0-4）：最大档不许漏文件名
     * ========================================================= */
    say('');
    say('[脱敏] 最大档下完整路径不许漏');
    await ev("(()=>{let n=0;while(MASK<2 && n++<5){document.querySelector('#btnMask').click();}return 0})()");
    await sleep(600);
    const maskLbl = await ev("document.querySelector('#btnMask').textContent");
    const paths = await ev("[...document.querySelectorAll('#rows .tr .fullpath')].map(e=>e.textContent)");
    const names = await ev("[...document.querySelectorAll('#rows .tr .nm')].map(e=>e.textContent)");
    const allTxt = paths.concat(names).join(' | ');
    say(`  档位 = ${maskLbl}`);
    say(`  展开路径样例 = ${paths[0] || '(无)'}`);
    A('脱敏到「路径＋文件名」档', /路径＋文件名/.test(maskLbl), maskLbl);
    A('展开路径不含真实目录名 whisper-large-v3',
      !allTxt.includes('whisper-large-v3'), '完整路径里漏了真实目录名');
    A('展开路径不含真实目录名 openvino',
      !allTxt.toLowerCase().includes('openvino'), '完整路径里漏了 openvino');
    A('展开路径仍保留盘符（还能看懂是哪块盘）', (paths[0] || '').startsWith('E:'), paths[0]);

    // 展开一个目录，看弹窗里的路径
    await ev("(()=>{const r=document.querySelector('#rows .tr[data-dir=\"1\"]');if(r)r.click();return 0})()");
    await sleep(1500);
    const exSub = await ev("document.querySelector('#exSub').textContent || ''");
    say(`  展开弹窗标题 = ${exSub}`);
    A('展开弹窗里的路径也脱敏了（P0-4 主案发点）',
      !exSub.includes('whisper-large-v3') && !exSub.toLowerCase().includes('openvino'), exSub);
    await ev("document.querySelector('#exClose').click()");

    // 回到「路径」档（直接设，别走点击取模 —— 2 点一下会绕回 0）
    await ev("(()=>{MASK=1;const b=document.querySelector('#btnMask');"
      + "b.textContent='脱敏：路径';b.classList.add('on');render();return MASK})()");
    await sleep(400);

    /* =========================================================
     * ⑥ 缓存回放选择（P1-3）
     * ========================================================= */
    say('');
    say('[回放] 载入上次结果能不能挑');
    const meta = await jget('/api/caches?meta=1');
    const m0 = (meta.caches || [])[0];
    say(`  /api/caches?meta=1 首项 = ${JSON.stringify(m0).slice(0, 200)}`);
    A('/api/caches?meta=1 返回结构化条目', m0 && typeof m0 === 'object' && !!m0.file, typeof m0);
    A('缓存条目带扫描根（root）', !!(m0 && m0.root), JSON.stringify(m0));
    A('缓存条目带创建时间', !!(m0 && m0.created), JSON.stringify(m0));
    A('缓存条目带文件数（可判断值不值得载）', !!(m0 && m0.file_count != null), JSON.stringify(m0));
    A('缓存条目带总体积', !!(m0 && m0.total_size != null), JSON.stringify(m0));

    await ev("document.querySelector('#btnCache').click()");
    await sleep(1200);
    const dlg = await ev(`(()=>({
      open: !!document.querySelector('#cacheDlg.show'),
      rows: document.querySelectorAll('#cacheList .ditem').length,
      txt: (document.querySelector('#cacheList')||{textContent:''}).textContent.slice(0,120)
    }))()`);
    say(`  选择弹窗 = ${JSON.stringify(dlg).slice(0, 200)}`);
    A('点「载入上次结果」弹出可选列表', dlg.open && dlg.rows > 1, JSON.stringify(dlg));
    await ev("(()=>{const d=document.querySelector('#cacheDlg');if(d)d.classList.remove('show')})()");

    /* ---------- 收尾：截图存档 ---------- */
    await ev("document.querySelectorAll('.tabs button[data-v]')[0].click()");
    await sleep(400);
    const shot = async (name, full) => {
      const r = await cdp.send('Page.captureScreenshot',
        { format: 'png', captureBeyondViewport: !!full }, sessionId);
      fs.writeFileSync(path.join(OUT, name), Buffer.from(r.data, 'base64'));
    };
    await shot('verify-01-清单-修复后.png');
    await ev("document.querySelectorAll('.tabs button[data-v]')[1].click()"); await sleep(600);
    await shot('verify-02-构成-修复后.png');
    await ev("document.querySelectorAll('.tabs button[data-v]')[0].click()"); await sleep(300);
    await shot('verify-03-全页.png', true);

  } catch (e) {
    A('脚本全程没崩', false, (e && (e.stack || e.message) || String(e)).slice(0, 400));
  } finally {
    try { if (ws) ws.close(); } catch (e) {}
    try { child.kill(); } catch (e) {}
    try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}

    say('');
    say(`结果：${pass} 通过 / ${fail} 失败（共 ${pass + fail}）`);
    if (fails.length) { say('失败项：'); fails.forEach(f => say('  · ' + f)); }
    fs.writeFileSync(path.join(OUT, 'ui_verify_last.txt'), L.join('\n') + '\n', 'utf8');
    process.stdout.write(`ui-verify: ${pass} pass / ${fail} fail -> ${path.join(OUT, 'ui_verify_last.txt')}\n`);
    process.exit(fail ? 1 : 0);
  }
})();
