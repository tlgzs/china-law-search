// 国家规章库采集器（无头浏览器路线，单并发 + 断点续传，仅落盘不入库）
// 用法：
//   node tools/gjgzk_scrape.mjs --limit 200        # 先跑 200 篇
//   node tools/gjgzk_scrape.mjs                     # 续跑全量（从 checkpoint 继续）
//   node tools/gjgzk_scrape.mjs --detail none       # 不抓详情页 HTML，只存列表 JSON + 正文
import { chromium } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const LIST_URL = 'https://www.gov.cn/zhengce/xxgk/gjgzk/index.htm?searchWord=';
const OUT_DIR = path.resolve('inbox/gjgzk');
const RAW_DIR = path.join(OUT_DIR, 'raw');
const DOCS_DIR = path.join(OUT_DIR, 'docs');
const CHECKPOINT = path.join(OUT_DIR, 'checkpoint.json');
const META_JSONL = path.join(OUT_DIR, 'meta.jsonl');
const STATS_FILE = path.join(OUT_DIR, 'run_stats.json');
const SEEN_URLS = path.join(OUT_DIR, 'seen_urls.txt');
const DUP_JSONL = path.join(OUT_DIR, 'duplicates.jsonl');

// 字段映射：来源接口的匿名列名 -> 业务含义
const F = {
  title: 'f_202321360426',
  category: 'f_202321807875',
  text: 'f_202321758948',
  url: 'doc_pub_url',
  sourceSite: 'f_202323394765',
  updateTime: 'f_202321915922',
  pubInfoDept: 'f_202344311304',
  pubInfoLocal: 'f_202321136868',
  region: 'f_20232380533',
  province: 'f_202321423473',
  issuingBody: 'f_20232151076',
  deptNames: 'f_202328191239',
  localId: 'f_202321124775',
};

const ARTICLE_RE = /第[一二三四五六七八九十百千零〇0-9]{1,8}条/g;

const args = process.argv.slice(2);
const getArg = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};
const LIMIT = Number(getArg('limit', '0')) || 0; // 0 表示不限量
const PAGE_SIZE = Number(getArg('pageSize', '100'));
const DETAIL_MODE = getArg('detail', 'govcn'); // govcn | all | none
const MIN_INTERVAL = 1500;
const JITTER = 900;

fs.mkdirSync(RAW_DIR, { recursive: true });
fs.mkdirSync(DOCS_DIR, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// 请求间隔 1.5s + 0~0.9s 随机抖动，单并发，避免把源站 IP 打限
const politeWait = () => sleep(MIN_INTERVAL + Math.floor(Math.random() * JITTER));

const stripHtml = (s) => (s == null ? '' : String(s).replace(/<[^>]+>/g, '').trim());

// URL 归一化：去协议、去尾斜杠、转小写，用于跨 http/https 的同文去重
const normUrl = (u) => stripHtml(u).replace(/^https?:\/\//i, '').replace(/\/+$/, '').toLowerCase();

// 已见 URL 集合：优先读 seen_urls.txt，缺失时从已落盘 meta 重建，保证续跑不重复
const loadSeenUrls = () => {
  const seen = new Set();
  if (fs.existsSync(SEEN_URLS)) {
    for (const line of fs.readFileSync(SEEN_URLS, 'utf8').split('\n')) {
      if (line.trim()) seen.add(line.trim());
    }
    return seen;
  }
  if (fs.existsSync(META_JSONL)) {
    for (const line of fs.readFileSync(META_JSONL, 'utf8').split('\n')) {
      if (!line.trim()) continue;
      try {
        const u = normUrl(JSON.parse(line).url);
        if (u) seen.add(u);
      } catch {
        // 跳过坏行
      }
    }
  }
  return seen;
};

const readCheckpoint = () => {
  if (!fs.existsSync(CHECKPOINT)) {
    return { version: 1, pageSize: PAGE_SIZE, lastPageDone: 0, collected: 0, total: 0, startedAt: new Date().toISOString(), updatedAt: null };
  }
  return JSON.parse(fs.readFileSync(CHECKPOINT, 'utf8'));
};

const writeJsonAtomic = (file, obj) => {
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(obj, null, 2), 'utf8');
  fs.renameSync(tmp, file);
};

// 从记录推导稳定 ID：优先本地库 ID，其次 gov.cn content 编号，最后对 URL 取哈希
const docIdOf = (item) => {
  const localId = item[F.localId];
  if (localId && String(localId).trim()) return `l${String(localId).trim()}`;
  const url = stripHtml(Array.isArray(item[F.url]) ? item[F.url][0] : item[F.url]);
  const m = url.match(/content_(\d+)/);
  if (m) return `g${m[1]}`;
  return `h${crypto.createHash('sha1').update(url).digest('hex').slice(0, 12)}`;
};

const normalize = (item) => {
  const url = stripHtml(Array.isArray(item[F.url]) ? item[F.url][0] : item[F.url] || '');
  const text = stripHtml(item[F.text]);
  const articles = (text.match(ARTICLE_RE) || []).length;
  return {
    doc_id: docIdOf(item),
    category: stripHtml(Array.isArray(item[F.category]) ? item[F.category][0] : item[F.category]),
    title: stripHtml(item[F.title]),
    url,
    source_site: stripHtml(item[F.sourceSite]),
    province: stripHtml(item[F.province]),
    region: stripHtml(item[F.region]),
    issuing_body: stripHtml(item[F.issuingBody]),
    dept_names: (Array.isArray(item[F.deptNames]) ? item[F.deptNames].map(stripHtml).join('|') : stripHtml(item[F.deptNames])),
    pub_info: stripHtml(item[F.pubInfoDept] || item[F.pubInfoLocal]),
    update_time: stripHtml(item[F.updateTime]),
    text_len: text.length,
    article_count: articles,
    status_code: item['f_202321864401'],
    fetched_at: new Date().toISOString(),
  };
};

const browser = await chromium.launch({ channel: 'chrome', headless: true });
const ctx = await browser.newContext({ locale: 'zh-CN' });
const page = await ctx.newPage();

// 打开列表页，抓一次真实请求，拿到接口地址与 athenaAppKey（不逆向算法，直接复用浏览器内的凭证）
let captured = null;
page.on('request', (req) => {
  if (/athena\/forward/.test(req.url()) && req.postData()?.includes('t_1860c735d31') && !captured) {
    captured = { url: req.url(), headers: req.headers(), body: req.postData() };
  }
});
console.log('[init] 打开列表页，等待前端渲染并捕获接口凭证…');
await page.goto(LIST_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForTimeout(6000);
if (!captured) {
  console.error('[BLOCKED] 未能捕获列表接口请求，页面可能改版或前端脚本未执行');
  await browser.close();
  process.exit(2);
}

const apiUrl = captured.url;
const template = JSON.parse(captured.body);
const apiHeaders = {
  'content-type': 'application/json;charset=UTF-8',
  accept: 'application/json, text/javascript, */*; q=0.01',
  referer: 'https://www.gov.cn/',
  athenaappname: captured.headers['athenaappname'],
  athenaappkey: captured.headers['athenaappkey'],
  'user-agent': captured.headers['user-agent'],
};

const fetchPage = async (pageNo) => {
  const res = await ctx.request.post(apiUrl, {
    headers: apiHeaders,
    data: { ...template, pageNo, pageSize: PAGE_SIZE },
    timeout: 40000,
  });
  if (!res.ok()) throw new Error(`HTTP ${res.status()} pageNo=${pageNo}`);
  const json = await res.json();
  if (json?.resultCode?.code !== 200) throw new Error(`resultCode=${json?.resultCode?.code} pageNo=${pageNo}`);
  return json.result.data;
};

const fetchDetailHtml = async (url) => {
  if (!url) return { html: null, status: 'no_url' };
  const isGovcn = /(^https?:\/\/)?([a-z0-9-]+\.)*gov\.cn\//i.test(url);
  if (DETAIL_MODE === 'none') return { html: null, status: 'skipped' };
  if (DETAIL_MODE === 'govcn' && !isGovcn) return { html: null, status: 'external_source' };
  try {
    const res = await ctx.request.get(url, {
      timeout: 40000,
      headers: { 'user-agent': apiHeaders['user-agent'], referer: 'https://www.gov.cn/' },
    });
    if (!res.ok()) return { html: null, status: `http_${res.status()}` };
    const buf = Buffer.from(await res.body());
    return { html: buf.toString('utf8'), status: 'ok' };
  } catch (e) {
    return { html: null, status: `error_${String(e.message).slice(0, 40)}` };
  }
};

const ck = readCheckpoint();
if (ck.collected > 0) {
  console.log(`[resume] checkpoint: 已完成 ${ck.lastPageDone} 页 / 累计 ${ck.collected} 篇，继续往下抓`);
}

let collected = ck.collected;
let pageNo = ck.lastPageDone + 1;
const seenUrls = loadSeenUrls();
let stats = { docs: 0, duplicates: 0, withArticles: 0, noArticles: 0, shell: 0, htmlOk: 0, htmlFail: 0, byCategory: {}, zeroArticleSamples: [], htmlFailSamples: [] };

while (true) {
  if (LIMIT && collected >= LIMIT) break;
  let data;
  try {
    data = await fetchPage(pageNo);
  } catch (e) {
    console.error(`[error] page ${pageNo}: ${e.message}`);
    ck.updatedAt = new Date().toISOString();
    ck.lastError = { pageNo, message: e.message, at: ck.updatedAt };
    writeJsonAtomic(CHECKPOINT, ck);
    await browser.close();
    process.exit(3);
  }

  const total = data.pager?.total ?? 0;
  const pageCount = data.pager?.pageCount ?? 0;
  fs.writeFileSync(path.join(RAW_DIR, `page_${String(pageNo).padStart(4, '0')}.json`), JSON.stringify(data), 'utf8');

  const list = data.list || [];
  console.log(`[page ${pageNo}/${pageCount}] total=${total} 本页 ${list.length} 条`);

  for (const item of list) {
    if (LIMIT && collected >= LIMIT) break;
    const meta = normalize(item);
    const docId = meta.doc_id;
    const txtPath = path.join(DOCS_DIR, `${docId}.txt`);
    const metaPath = path.join(DOCS_DIR, `${docId}.meta.json`);

    if (fs.existsSync(txtPath) && fs.existsSync(metaPath)) {
      continue; // 断点续传：已落盘的跳过，不重复请求
    }

    // 同文去重：http/https、尾斜杠差异视为同一部规章
    const norm = normUrl(meta.url);
    if (norm && seenUrls.has(norm)) {
      stats.duplicates += 1;
      fs.appendFileSync(
        DUP_JSONL,
        JSON.stringify({ doc_id: docId, category: meta.category, title: meta.title, url: meta.url, normalized: norm, at: new Date().toISOString() }) + '\n',
        'utf8'
      );
      continue;
    }
    if (norm) {
      seenUrls.add(norm);
      fs.appendFileSync(SEEN_URLS, `${norm}\n`, 'utf8');
    }

    const rawText = stripHtml(item[F.text]);
    fs.writeFileSync(txtPath, rawText, 'utf8');
    fs.writeFileSync(metaPath, JSON.stringify({ ...meta, raw_record: item }, null, 2), 'utf8');

    const det = await fetchDetailHtml(meta.url);
    if (det.html) {
      fs.writeFileSync(path.join(DOCS_DIR, `${docId}.html`), det.html, 'utf8');
    }
    const withHtml = { ...meta, html_status: det.status };
    fs.appendFileSync(META_JSONL, JSON.stringify(withHtml) + '\n', 'utf8');

    collected += 1;
    stats.docs += 1;
    stats.byCategory[meta.category] = (stats.byCategory[meta.category] || 0) + 1;
    if (meta.article_count > 0) stats.withArticles += 1;
    else {
      stats.noArticles += 1;
      if (stats.zeroArticleSamples.length < 10) stats.zeroArticleSamples.push(`${meta.category}|${meta.title}`);
    }
    if (meta.text_len < 200) stats.shell += 1;
    if (det.status === 'ok' || det.status === 'external_source' || det.status === 'skipped') stats.htmlOk += 1;
    else {
      stats.htmlFail += 1;
      if (stats.htmlFailSamples.length < 10) stats.htmlFailSamples.push(`${docId}|${det.status}|${meta.url}`);
    }

    // 只有真正发了详情页请求才需要限速；纯落盘不占用源站
    if (DETAIL_MODE !== 'none') await politeWait();
  }

  ck.lastPageDone = pageNo;
  ck.collected = collected;
  ck.total = total;
  ck.pageCount = pageCount;
  ck.pageSize = PAGE_SIZE;
  ck.updatedAt = new Date().toISOString();
  writeJsonAtomic(CHECKPOINT, ck);

  if (pageNo >= pageCount) break;
  pageNo += 1;
  if (LIMIT && collected >= LIMIT) break;
  await politeWait();
}

stats.collected = collected;
stats.limit = LIMIT;
stats.pageSize = PAGE_SIZE;
stats.detailMode = DETAIL_MODE;
stats.finishedAt = new Date().toISOString();
writeJsonAtomic(STATS_FILE, stats);

console.log('\n=== 本次运行统计 ===');
console.log(JSON.stringify(stats, null, 2));
await browser.close();
