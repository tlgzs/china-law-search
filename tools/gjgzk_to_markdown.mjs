// 将已落盘的规章数据转成每部一份的 markdown，便于人眼复核与 diff
// 用法：node tools/gjgzk_to_markdown.mjs
import fs from 'node:fs';
import path from 'node:path';

const BASE = path.resolve('inbox/gjgzk');
const DOCS = path.join(BASE, 'docs');
const OUT = path.join(BASE, 'md');

fs.mkdirSync(OUT, { recursive: true });

const safeName = (s) =>
  (s || 'untitled').replace(/[\\/:*?"<>|\r\n]/g, '_').replace(/\s+/g, ' ').trim().slice(0, 80);

const cnToDate = (y, m, d) => `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;

// 从公布信息串里取公布日期、施行日期、令号；取不到返回 null，不猜
const parsePubInfo = (info) => {
  const s = info || '';
  const pub = s.match(/(\d{4})年(\d{1,2})月(\d{1,2})日[^。；]{0,10}?(公布|发布|通过)/);
  const eff = s.match(/自(\d{4})年(\d{1,2})月(\d{1,2})日起施行/);
  const order = s.match(/([^\s，,。；（）]{0,18}令\s*(?:[〔\[]\d{4}[〕\]]\s*)?第\s*[0-9]+\s*号)/);
  return {
    publishDate: pub ? cnToDate(pub[1], pub[2], pub[3]) : null,
    effectiveDate: eff ? cnToDate(eff[1], eff[2], eff[3]) : null,
    orderNo: order ? order[1].replace(/\s+/g, '') : null,
  };
};

const issueBodyOf = (m) => (m.issuing_body || m.dept_names || '').replace(/\|/g, '、') || null;

const files = fs.readdirSync(DOCS).filter((f) => f.endsWith('.meta.json'));
let ok = 0;
const failed = [];

for (const f of files) {
  const metaPath = path.join(DOCS, f);
  const docId = f.replace('.meta.json', '');
  const txtPath = path.join(DOCS, `${docId}.txt`);
  try {
    if (!fs.existsSync(txtPath)) throw new Error('缺少 txt');
    const m = JSON.parse(fs.readFileSync(metaPath, 'utf8'));
    const body = fs.readFileSync(txtPath, 'utf8').trim();
    if (!body) throw new Error('正文为空');

    // 公布信息：优先元数据字段，缺失时从正文开头括号里取
    const pubInfo = m.pub_info || (body.match(/（[^（）]{10,120}?公布[^（）]*）/)?.[0] || '').replace(/[（）]/g, '');
    const { publishDate, effectiveDate, orderNo } = parsePubInfo(pubInfo);

    const md = [
      `# ${m.title || docId}`,
      '',
      `- 标题：${m.title || ''}`,
      `- 发布机关：${issueBodyOf(m) || ''}`,
      `- 令号：${orderNo || ''}`,
      `- 公布日期：${publishDate || ''}`,
      `- 施行日期：${effectiveDate || ''}`,
      `- 栏目：${m.category || ''}`,
      `- 原始 URL：${m.url || ''}`,
      `- doc_id：${docId}`,
      '',
      '## 正文',
      '',
      body,
      '',
    ].join('\n');

    fs.writeFileSync(path.join(OUT, `${docId}_${safeName(m.title)}.md`), md, 'utf8');
    ok += 1;
  } catch (e) {
    failed.push({ docId, reason: e.message });
  }
}

const size = fs.readdirSync(OUT).reduce((a, n) => a + fs.statSync(path.join(OUT, n)).size, 0);
console.log(`生成 ${ok} 份 md，失败 ${failed.length} 份，总体积 ${(size / 1024 / 1024).toFixed(1)} MB`);
if (failed.length) console.log('失败明细：', JSON.stringify(failed.slice(0, 20), null, 2));
