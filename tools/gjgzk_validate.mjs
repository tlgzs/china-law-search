// 校验已落盘的规章样本：条号连续性、正文完整性、空壳/降级识别、正文与详情页一致性
import fs from 'node:fs';
import path from 'node:path';

const DOCS = path.resolve('inbox/gjgzk/docs');
const META = path.resolve('inbox/gjgzk/meta.jsonl');

// 中文数字转整数（按位权处理，支持 一千零三 / 一百零二 等）
const cnToInt = (s) => {
  if (/^\d+$/.test(s)) return Number(s);
  const digit = { 零: 0, 〇: 0, 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9 };
  const unit = { 十: 10, 百: 100, 千: 1000 };
  let total = 0;
  let section = 0;
  let num = 0;
  for (const ch of s) {
    if (ch in digit) num = digit[ch];
    else if (ch in unit) {
      section += (num === 0 ? 1 : num) * unit[ch];
      num = 0;
    } else if (ch === '万') {
      total += (section + num) * 10000;
      section = 0;
      num = 0;
    }
  }
  return total + section + num;
};

const clean = (t) => t.replace(/\s+/g, '');
const htmlToText = (html) =>
  clean(
    html
      .replace(/<script[\s\S]*?<\/script>/gi, ' ')
      .replace(/<style[\s\S]*?<\/style>/gi, ' ')
      .replace(/<[^>]+>/g, ' ')
      .replace(/&nbsp;/g, ' ')
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      .replace(/&amp;/g, '&')
      .replace(/&quot;/g, '"')
      .replace(/&#(\d+);/g, (_, d) => String.fromCharCode(Number(d)))
  );

const metas = fs.readFileSync(META, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l));
const rows = [];
for (const m of metas) {
  const txt = fs.readFileSync(path.join(DOCS, `${m.doc_id}.txt`), 'utf8');
  const nums = [...txt.matchAll(/第([一二三四五六七八九十百千零〇0-9]{1,8})条/g)]
    .map((x) => cnToInt(x[1]))
    .filter((n) => n > 0 && n < 100000);
  const uniq = [...new Set(nums)].sort((a, b) => a - b);
  const realGaps = [];
  for (let i = 1; i < uniq.length; i++) {
    if (uniq[i] !== uniq[i - 1] + 1 && uniq[i] - uniq[i - 1] < 100) realGaps.push(`${uniq[i - 1]}→${uniq[i]}`);
  }
  const tailOk = /[。！？；”』】\)）]$/.test(txt.trim().slice(-1));

  // 详情页一致性：正文尾部 30 字是否出现在详情页文本里
  let htmlCheck = 'no_html';
  const htmlPath = path.join(DOCS, `${m.doc_id}.html`);
  if (m.html_status === 'ok' && fs.existsSync(htmlPath)) {
    const h = htmlToText(fs.readFileSync(htmlPath, 'utf8'));
    const tail = clean(txt).slice(-30);
    const head = clean(txt).slice(0, 30);
    const hitTail = h.includes(tail.slice(-20));
    const hitHead = h.includes(head.slice(0, 20));
    htmlCheck = hitTail && hitHead ? 'match' : hitTail || hitHead ? 'partial' : 'mismatch';
  }

  rows.push({
    id: m.doc_id,
    cat: m.category,
    title: m.title,
    len: txt.length,
    arts: uniq.length,
    maxArt: uniq[uniq.length - 1] || 0,
    gaps: realGaps,
    tailOk,
    html: m.html_status,
    htmlCheck,
  });
}

const byCat = {};
for (const r of rows) byCat[r.cat] = (byCat[r.cat] || 0) + 1;
const htmlStats = {};
for (const r of rows) htmlStats[r.html] = (htmlStats[r.html] || 0) + 1;
const checkStats = {};
for (const r of rows) checkStats[r.htmlCheck] = (checkStats[r.htmlCheck] || 0) + 1;

console.log('=== 样本 ===', rows.length, JSON.stringify(byCat));
console.log('带「第X条」:', rows.filter((r) => r.arts > 0).length, '/ 无:', rows.filter((r) => r.arts === 0).length);
console.log('空壳(<200 字):', rows.filter((r) => r.len < 200).length);
console.log('条号真跳号:', rows.filter((r) => r.gaps.length > 0).length);
console.log('尾部非句末标点:', rows.filter((r) => !r.tailOk).length);
console.log('详情 HTML 状态:', JSON.stringify(htmlStats));
console.log('正文↔详情页一致性:', JSON.stringify(checkStats));
console.log('长度分档:', JSON.stringify({ '<500': rows.filter((r) => r.len < 500).length, '500-2000': rows.filter((r) => r.len >= 500 && r.len < 2000).length, '2000-8000': rows.filter((r) => r.len >= 2000 && r.len < 8000).length, '>=8000': rows.filter((r) => r.len >= 8000).length }));

console.log('\n=== 条号跳号明细 ===');
for (const r of rows.filter((x) => x.gaps.length)) console.log(`- ${r.id} ${r.title} | ${r.gaps.join(',')} | max=${r.maxArt}`);
console.log('\n=== 尾部异常明细 ===');
for (const r of rows.filter((x) => !x.tailOk)) console.log(`- ${r.id} ${r.title} | ...${r.len}字`);
console.log('\n=== 正文与详情页不一致 ===');
for (const r of rows.filter((x) => x.htmlCheck === 'mismatch' || x.htmlCheck === 'partial')) console.log(`- ${r.id} ${r.title} | ${r.htmlCheck}`);
