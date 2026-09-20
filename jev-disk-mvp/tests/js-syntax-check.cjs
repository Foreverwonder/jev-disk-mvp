/* 抽 index.html 里的 <script> 内容，交给 node --check 验语法。
   用法: node js-syntax-check.cjs <index.html>  → 输出 OK/ERR 与明细文件 */
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const file = process.argv[2];
const src = fs.readFileSync(file, 'utf8');
const out = [];
const re = /<script\b[^>]*>([\s\S]*?)<\/script>/gi;
let m, n = 0, bad = 0;
while ((m = re.exec(src)) !== null) {
  n++;
  const body = m[1];
  if (!body.trim()) { out.push(`script#${n}: (空，跳过)`); continue; }
  const tmp = path.join(process.env.TEMP, `uf_syn_${n}.js`);
  fs.writeFileSync(tmp, body, 'utf8');
  const r = spawnSync(process.execPath, ['--check', tmp], { encoding: 'utf8' });
  if (r.status === 0) { out.push(`script#${n}: OK  (${body.length} 字符)`); }
  else { bad++; out.push(`script#${n}: ERR\n${(r.stderr || '').split('\n').slice(0, 8).join('\n')}`); }
  try { fs.unlinkSync(tmp); } catch (e) {}
}
out.unshift(`scripts=${n} errors=${bad}`);
const txt = out.join('\n') + '\n';
fs.writeFileSync(path.join(process.env.TEMP, 'uf_js_syntax.txt'), txt, 'utf8');
process.stdout.write(txt);
process.exit(bad ? 1 : 0);
