/**
 * 报告渲染校验（需要服务在跑）：取真实病例的报告 JSON，用患者端的前端渲染器渲染一遍，
 * 断言 13 段里既没有 [object Object]，也没有内部英文键泄漏 —— 即"用户在报告页上真正看到的东西"。
 *
 * 运行：python app.py（另开一个终端）→ node verify_report_render.mjs
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const BASE = 'http://127.0.0.1:5000';
const HERE = path.dirname(fileURLToPath(import.meta.url));
const js = fs.readFileSync(path.join(HERE, 'templates', 'patient.html'), 'utf8');
const inline = [...js.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n;\n');

const els = new Map();
const el = id => {
  if (!els.has(id)) els.set(id, { innerHTML: '', textContent: '', style: { setProperty() { }, width: '' },
    classList: { add() { }, remove() { }, toggle() { }, contains: () => false }, dataset: {},
    setAttribute() { }, getAttribute: () => null, addEventListener() { }, querySelector: () => null,
    querySelectorAll: () => [], focus() { }, click() { } });
  return els.get(id);
};
globalThis.document = { getElementById: el, querySelector: () => null, querySelectorAll: () => [],
  addEventListener() { }, title: '', fonts: { ready: Promise.resolve() } };
globalThis.window = { addEventListener() { }, scrollTo() { }, innerHeight: 900, scrollY: 0,
  matchMedia: () => ({ matches: false }), localStorage: { getItem: () => null, setItem() { }, removeItem() { } } };
globalThis.localStorage = globalThis.window.localStorage;
globalThis.IntersectionObserver = class { observe() { } unobserve() { } disconnect() { } };
globalThis.location = { href: '', reload() { } };
globalThis.alert = () => { };

eval(inline + '\n;globalThis.__R = { renderSectionValue };');
const { renderSectionValue } = globalThis.__R;

// 取真实病例
const patient = await (await fetch(BASE + '/api/login', {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ username: 'patient', password: 'patient' }),
})).json();
const token = patient.session.token;
const casesResp = await (await fetch(BASE + '/api/v2/cases', {
  headers: { Authorization: 'Bearer ' + token },
})).json();
const caseId = (casesResp.cases || [])[0]?.case_id || (casesResp.error ? null : null);

let target = caseId;
if (!target) {
  // 没有病例就现建一个
  const created = await (await fetch(BASE + '/api/v2/case', {
    method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },
    body: JSON.stringify({ medical_history: '患者：李*，男，61岁。主诉：视物模糊3月，双下肢浮肿1周。既往2型糖尿病5年，尿蛋白++。',
                           main_issue: '该看哪个科', age: 61, sex: '男' }),
  })).json();
  target = created.case_id;
}
console.log('核验病例：', target);

const report = await (await fetch(`${BASE}/api/v2/case/${target}/report`, {
  headers: { Authorization: 'Bearer ' + token },
})).json();
const sections = (report.full || {}).sections || {};
console.log('段落数：', Object.keys(sections).length);

let problems = [];
for (const [name, value] of Object.entries(sections)) {
  const html = renderSectionValue(value);
  if (html.includes('[object Object]')) problems.push(`${name}: 出现 [object Object]`);
  if (/undefined|NaN/.test(html)) problems.push(`${name}: 出现 undefined/NaN`);
  for (const key of ['judgement', 'evidence_state', 'supports', 'confidence', 'step_id', 'scope']) {
    if (html.includes('>' + key + '<')) problems.push(`${name}: 泄漏英文键 ${key}`);
  }
  console.log(`  · ${name}：渲染 ${html.length} 字符${html.includes('[object Object]') ? '  ❌' : '  ✅'}`);
}

const sample = renderSectionValue(sections['证据关联']);
console.log('\n证据关联渲染样例（前 600 字）：\n' + sample.replace(/\s+/g, ' ').slice(0, 600));
console.log('\n结论：' + (problems.length ? '❌ 仍有问题\n - ' + problems.join('\n - ') : '✅ 全部段落渲染正常，无 [object Object]、无英文键泄漏'));
process.exit(problems.length ? 1 : 0);
