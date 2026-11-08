// 全量采集收口统计：总条数、分类、去重、条号命中与缺口、体积
import fs from 'node:fs';
import path from 'node:path';

const BASE = path.resolve('inbox/gjgzk');
const DOCS = path.join(BASE, 'docs');

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

// G7 口径：从第一条起按递增链推进，允许 ≤5 条的缺口（记为缺条），更大的跳号视为正文引用而非条号
const analyzeArticles = (text) => {
  const nums = [...text.matchAll(/第([一二三四五六七八九十百千零〇0-9]{1,8})条/g)].map((x) => cnToInt(x[1]));
  let prev = 0;
  const gaps = [];
  let hasFirst = false;
  for (const n of nums) {
    if (n === prev + 1) {
      hasFirst = hasFirst || n === 1;
      prev = n;
    } else if (n > prev && n - prev <= 5) {
      gaps.push(`${prev}→${n}`);
      prev = n;
    }
  }
  return { articles: prev, gaps, hasFirst };
};

const files = fs.readdirSync(DOCS).filter((f) => f.endsWith('.meta.json'));
const byCat = {};
let withArticles = 0;
let shell = 0;
const gapDocs = [];
const zeroArticleDocs = [];

for (const f of files) {
  const docId = f.replace('.meta.json', '');
  const m = JSON.parse(fs.readFileSync(path.join(DOCS, f), 'utf8'));
  const text = fs.readFileSync(path.join(DOCS, `${docId}.txt`), 'utf8');
  byCat[m.category] = (byCat[m.category] || 0) + 1;
  if (text.length < 200) shell += 1;
  const a = analyzeArticles(text);
  if (a.articles > 0) withArticles += 1;
  else zeroArticleDocs.push(`${docId}|${m.category}|${m.title}`);
  if (a.gaps.length) gapDocs.push(`${docId}|${m.category}|${m.title}|${a.gaps.join(',')}`);
}

let dupCount = 0;
const dupPath = path.join(BASE, 'duplicates.jsonl');
if (fs.existsSync(dupPath)) dupCount = fs.readFileSync(dupPath, 'utf8').trim().split('\n').filter(Boolean).length;

const rawDir = path.join(BASE, 'raw');
const pageFiles = fs.readdirSync(rawDir).filter((n) => n.endsWith('.json'));
const firstPage = JSON.parse(fs.readFileSync(path.join(rawDir, pageFiles.sort()[0]), 'utf8'));
const listTotal = firstPage.pager?.total ?? 0;
const lastPage = JSON.parse(fs.readFileSync(path.join(rawDir, pageFiles.sort().at(-1)), 'utf8'));
const lastPageNo = lastPage.pager?.pageNo ?? 0;
const pageCount = lastPage.pager?.pageCount ?? 0;

const dirSize = (d) => fs.readdirSync(d).reduce((a, n) => a + fs.statSync(path.join(d, n)).size, 0);
const sizeMb = (dirSize(BASE) / 1024 / 1024).toFixed(1);

console.log(JSON.stringify({
  listTotal,
  pagesFetched: `${lastPageNo}/${pageCount}`,
  docsSaved: files.length,
  byCategory: byCat,
  duplicatesSkipped: dupCount,
  withArticles,
  articleHitRate: `${((withArticles / files.length) * 100).toFixed(1)}%`,
  zeroArticle: zeroArticleDocs.length,
  shell,
  gapDocs: gapDocs.length,
  sizeMb,
}, null, 2));

if (zeroArticleDocs.length) {
  console.log('\n=== 无条号文档（前20）===');
  console.log(zeroArticleDocs.slice(0, 20).join('\n'));
}
if (gapDocs.length) {
  console.log(`\n=== 条号不连续（${gapDocs.length} 部，源侧缺条，前30）===`);
  console.log(gapDocs.slice(0, 30).join('\n'));
}
fs.writeFileSync(path.join(BASE, 'summary.json'), JSON.stringify({ listTotal, docsSaved: files.length, byCategory: byCat, duplicatesSkipped: dupCount, withArticles, zeroArticle: zeroArticleDocs.length, shell, gapDocs: gapDocs.length, sizeMb, generatedAt: new Date().toISOString() }, null, 2), 'utf8');
