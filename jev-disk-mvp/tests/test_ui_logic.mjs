/*
 * 纯逻辑回归测试（可用性修复轮）
 * ---------------------------------------------------------------
 * 为什么这么写：界面逻辑全在 index.html 的单文件 <script> 里，直接 import 会连
 * boot() 一起跑起来（要服务、要 key）。所以这里从源码里「抠」出目标函数，
 * 配上最小的依赖，在 Node 里用真实路径数据跑断言。
 *
 * 好处：改完之后，函数要是被删了、被改名了、或者行为漂了，这里当场红。
 *
 * 跑法：  node tests/test_ui_logic.mjs
 * 结果：  同时写到 tests/ui_logic_last.txt（避免中文过 PowerShell 管道变乱码）
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HTML = path.join(HERE, '..', 'index.html');
const src = fs.readFileSync(HTML, 'utf8');

const out = [];
const log = s => { out.push(s); };
let pass = 0, fail = 0;
const failures = [];

function check(name, cond, detail) {
  if (cond) { pass++; log(`  ok   ${name}`); }
  else { fail++; failures.push(name); log(`  FAIL ${name}${detail ? '  <<< ' + detail : ''}`); }
}
function eq(name, got, want) {
  check(name, got === want, `got=${JSON.stringify(got)} want=${JSON.stringify(want)}`);
}

/* ---------- 从 index.html 抠函数（大括号配平，跳过字符串与注释） ---------- */
function grab(header) {
  const i = src.indexOf(header);
  if (i < 0) return null;
  // 找函数体第一个 {
  let j = src.indexOf('{', i);
  if (j < 0) return null;
  let depth = 0, k = j, str = null, lineComment = false, blockComment = false;
  for (; k < src.length; k++) {
    const c = src[k], n = src[k + 1];
    if (lineComment) { if (c === '\n') lineComment = false; continue; }
    if (blockComment) { if (c === '*' && n === '/') { blockComment = false; k++; } continue; }
    if (str) {
      if (c === '\\') { k++; continue; }
      if (c === str) str = null;
      continue;
    }
    if (c === '/' && n === '/') { lineComment = true; k++; continue; }
    if (c === '/' && n === '*') { blockComment = true; k++; continue; }
    if (c === '"' || c === "'" || c === '`') { str = c; continue; }
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) return src.slice(i, k + 1); }
  }
  return null;
}

/* ---------- 沙箱：把抠出来的函数接上最小依赖再求值 ---------- */
function makeSandbox(names, globals, prelude) {
  const parts = [];
  for (const n of names) {
    const body = grab(`function ${n}(`);
    if (!body) throw new Error(`index.html 里找不到函数 ${n}()`);
    parts.push(body);
  }
  const decl = Object.entries(globals)
    .map(([k, v]) => `var ${k} = ${JSON.stringify(v)};`).join('\n');
  const call = `return {${names.join(', ')}};`;
  // eslint-disable-next-line no-new-func
  return new Function(`${decl}\n${prelude || ''}\n${parts.join('\n')}\n${call}`)();
}

/* mask() 在源码里是箭头函数（grab 只认 function 声明），原样抠出来当 prelude，
   保证测的是真身而不是我重写的一个近似版。 */
const maskIdx = src.indexOf('const mask = s =>');
if (maskIdx < 0) throw new Error('index.html 里找不到 mask 箭头函数');
const MASK_LINE = src.slice(maskIdx, src.indexOf('\n', maskIdx));

log('== JEV 硬盘体检 · 界面纯逻辑测试 ==');
log(`源文件：${HTML.replace(/.*[\\/]/, '')}  ${src.length} 字符`);
log('');

/* ============================================================
 * T1 脱敏：完整路径必须被打码（P0-4）
 * 用户实测：脱敏开到最大档，名字列打码了，展开的完整路径没打，
 *          抓到的原文里带着真实文件名 + 一个微信 wxid。
 * ============================================================ */
log('[T1] maskPath —— 脱敏必须盖住完整路径里的文件名');
try {
  const HOME_REAL = 'C:\\Users\\alice';
  const P = 'C:\\Users\\alice\\AppData\\Roaming\\WeChat\\wxid_****\\db.sqlite';

  const mk = m => makeSandbox(['maskPath', 'maskName'],
    { MASK: m, MASK_KEEP: 4, HOME: HOME_REAL }, MASK_LINE);

  // MASK=0：全关，原样
  const s0 = mk(0);
  eq('MASK=0 时原样输出', s0.maskPath(P), P);

  // MASK=1：只盖用户名（向后兼容，老行为）
  const s1 = mk(1);
  const r1 = s1.maskPath(P);
  check('MASK=1 盖掉用户名 alice', !r1.includes('alice'), `got=${r1}`);
  check('MASK=1 保留文件名（老行为不变）', r1.includes('db.sqlite'), `got=${r1}`);

  // MASK=2：路径 + 文件名，全盖
  const s2 = mk(2);
  const r2 = s2.maskPath(P);
  check('MASK=2 盖掉用户名', !r2.includes('alice'), `got=${r2}`);
  check('MASK=2 盖掉隐私 wxid', !r2.includes('wxid_****'), `got=${r2}`);
  check('MASK=2 盖掉中间目录名 WeChat', !r2.includes('WeChat'), `got=${r2}`);
  check('MASK=2 保留粗轮廓（长度>0 且含分隔符）', r2.length > 0 && r2.includes('\\'), `got=${r2}`);
  check('MASK=2 盘符不该被吃掉', r2.startsWith('C:'), `got=${r2}`);

  // 非 HOME 下的路径（扫 D 盘时的真实情况）
  const sD = mk(2);
  const rD = sD.maskPath('D:\\AI_Projects\\secret_client_name\\weights.bin');
  check('D 盘路径也要脱敏', !rD.includes('secret_client_name'), `got=${rD}`);
  check('D 盘路径保留盘符', rD.startsWith('D:'), `got=${rD}`);
} catch (e) { check('maskPath 可用', false, e.message); }

/* ============================================================
 * T2 按落点：要剥「本次扫描的根」，不是用户主目录（P0-3）
 * 用户实测：扫 D:\AI_Projects 时，所有条目都归到 "D:" 一格 —— 一根 100% 的柱子。
 * ============================================================ */
log('');
log('[T2] locOf —— 按落点必须相对「本次扫描的根」');
try {
  const HOME_REAL = 'C:\\Users\\alice';
  const ROOT_D = 'D:\\AI_Projects';
  const s = makeSandbox(['locOf'], { HOME: HOME_REAL, SCAN_ROOT: ROOT_D });

  eq('扫 D 盘：第一层目录要归到 venv',
     s.locOf('D:\\AI_Projects\\venv\\torch\\lib\\x.dll'), 'venv');
  eq('扫 D 盘：根下的文件归到「根目录下的文件」',
     s.locOf('D:\\AI_Projects\\a.txt'), '(根目录下的文件)');
  eq('扫 D 盘：不该整盘归成 D: 一格',
     s.locOf('D:\\AI_Projects\\models\\a.bin'), 'models');

  // 没设扫描根时，退回剥主目录（回放老缓存、或还没扫过的情况）
  const s2 = makeSandbox(['locOf'], { HOME: HOME_REAL, SCAN_ROOT: '' });
  eq('没有扫描根时退回剥主目录',
     s2.locOf('C:\\Users\\alice\\Downloads\\x.zip'), 'Downloads');

  // 扫描根就是主目录时，行为也要对
  const s3 = makeSandbox(['locOf'], { HOME: HOME_REAL, SCAN_ROOT: HOME_REAL });
  eq('扫主目录时第一层是 AppData',
     s3.locOf('C:\\Users\\alice\\AppData\\Local\\Temp\\a.tmp'), 'AppData');
} catch (e) { check('locOf 可用', false, e.message); }

/* ============================================================
 * T3 清单出口：搜索 + 动作筛选 + 分组折叠（P1-1）
 * 用户实测：560px 可视 / 68,094px 内容，要滚 122 屏，且没有筛选和搜索。
 * ============================================================ */
log('');
log('[T3] fltItems —— 清单必须能收窄（搜索 / 筛选 / 折叠）');
try {
  const mk = (name, p, verdict, size) => ({ name, path: p, verdict, size, kind: 'user_content', conf: 0.9 });
  const ITEMS = [
    mk('torch-2.1.0-cp311.whl', 'D:\\AI_Projects\\venv\\wheels\\torch-2.1.0-cp311.whl', '别碰', 900e6),
    mk('cache.bin', 'D:\\AI_Projects\\proj\\cache.bin', '建议删除', 500e6),
    mk('note.txt', 'D:\\AI_Projects\\docs\\note.txt', '慎重', 3e3),
    mk('old.whl', 'D:\\AI_Projects\\downloads\\old.whl', '可以考虑', 20e6),
  ];
  // 真实的 bucketOf / rc 一起抽出来 —— 免得测的是一个我仿写的近似版
  const s = makeSandbox(['fltItems', 'bucketOf', 'rc'],
    { COLLAPSED: [], UI: { warn_conf: 0.6, warn_size_gb: 5 },
      KIND_TX: { regenerable_cache: '可再生缓存', installer: '安装包', program_body: '程序本体',
                 app_state: '应用状态', user_content: '用户产出', build_output: '构建产物', unknown: '看不懂' } });
  const call = (q, acts) => s.fltItems(ITEMS, q, new Set(acts)).map(x => x.name);

  eq('空条件：全部返回', call('', ['删', '看', '留']).length, 4);
  eq('搜索 torch：只留 1 条', JSON.stringify(call('torch', ['删', '看', '留'])),
     JSON.stringify(['torch-2.1.0-cp311.whl']));
  eq('搜索路径片段 wheels 也能命中', call('wheels', ['删', '看', '留']).length, 1);
  eq('搜索大小写不敏感', call('TORCH', ['删', '看', '留']).length, 1);
  eq('只勾「可以删」：留 2 条（建议删除 + 可以考虑）',
     call('', ['删']).length, 2);
  eq('只勾「建议不清理」：留 1 条', call('', ['留']).join(), 'torch-2.1.0-cp311.whl');
  eq('搜索 + 筛选叠加', call('old', ['删']).join(), 'old.whl');
  /* 折叠不在这里测：折叠由渲染层负责（折叠掉的组要保留组头，否则再也点不开）。
     那条路径用真实浏览器测，见 tests/ui_verify.cjs。 */
} catch (e) { check('fltItems 可用', false, e.message); }

/* ---------- 汇总 ---------- */
log('');
log(`结果：${pass} 通过 / ${fail} 失败（共 ${pass + fail}）`);
if (failures.length) { log('失败项：'); failures.forEach(f => log('  · ' + f)); }

const report = out.join('\n') + '\n';
fs.writeFileSync(path.join(HERE, 'ui_logic_last.txt'), report, 'utf8');
process.stdout.write(`ui-logic: ${pass} pass / ${fail} fail  -> tests/ui_logic_last.txt\n`);
process.exit(fail ? 1 : 0);
