/**
 * Execute the dashboard's own inline script against a stub DOM and assert it
 * actually draws.
 *
 * `node --check` proves the script parses. It does not prove that a chart
 * renders: a selector typo, a scale that divides by zero, or an axis that
 * produces no ticks all parse perfectly and leave an empty <svg> on the page.
 * Nothing else in this project runs JavaScript, so without this the only thing
 * standing between a blank chart and a release is somebody opening the file.
 *
 * The stub is deliberately tiny — the script draws by assigning innerHTML, so
 * a handful of no-op nodes is enough to let it run to completion. The script
 * selects its first period on load, so the charts draw with no prompting.
 *
 * Usage: node .github/scripts/render_charts.js <dashboard.html>
 */
'use strict';
const fs = require('fs');

const file = process.argv[2];
if (!file) { console.error('usage: render_charts.js <dashboard.html>'); process.exit(2); }
const page = fs.readFileSync(file, 'utf8');

const scripts = [...page.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/g)];
const js = scripts.filter(m => !/type="application\/json"/.test(m[1])).map(m => m[2]);
const json = scripts.filter(m => /type="application\/json"/.test(m[1]));
if (js.length !== 1) { console.error(`expected 1 script block, found ${js.length}`); process.exit(1); }

const byId = {};
const listeners = [];
function node(id) {
  if (byId[id]) { return byId[id]; }
  const n = {
    id, innerHTML: '', textContent: '', style: {}, children: [],
    classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
    setAttribute() {}, getAttribute() { return null; },
    querySelector: () => node(id + ':child'),
    querySelectorAll: () => emptyList(),
    addEventListener(_, fn) { listeners.push(fn); },
    appendChild() {}, removeChild() {}, showModal() {}, close() {},
  };
  byId[id] = n;
  return n;
}
function emptyList() { const a = []; return a; }

// The period payload has to reach the script through the element it reads.
// Selected by id, not by position: the chart cards emit their own JSON blocks
// and sit after this one in the document.
const periodBlock = json.find(m => /id="period-data"/.test(m[1]));
if (!periodBlock) { console.error('no #period-data block in the page'); process.exit(1); }
node('period-data').textContent = periodBlock[2];

global.document = {
  getElementById: node,
  querySelectorAll: () => emptyList(),
  addEventListener() {},
  createElement: () => node('detached'),
};
global.window = global;

try { eval(js[0]); } catch (e) {
  console.error('the page script threw while running:', e && e.stack);
  process.exit(1);
}

const svg = byId['perf-chart'];
if (!svg) { console.error('the script never looked up #perf-chart'); process.exit(1); }
const html = svg.innerHTML || '';
const count = (re) => (html.match(re) || []).length;

const grid = count(/class="grid-line"/g);
const zero = count(/class="axis-zero"/g);
const yLabels = [...html.matchAll(/class="axis-label y">([^<]*)</g)].map(m => m[1]);
const xLabels = [...html.matchAll(/class="axis-label x">([^<]*)</g)].map(m => m[1]);
const marks = count(/class="(bar-(?:pos|neg)|cum-line)/g);

const problems = [];
if (!html.trim()) { problems.push('the chart rendered nothing at all'); }
if (grid < 2) { problems.push(`only ${grid} grid line(s) drawn`); }
if (zero !== 1) { problems.push(`expected one zero line, drew ${zero}`); }
if (yLabels.length < 3) { problems.push(`only ${yLabels.length} y-axis label(s)`); }
if (xLabels.length < 2) { problems.push(`only ${xLabels.length} dated x-axis label(s)`); }
if (!xLabels.every(l => /^[A-Z][a-z]{2} \d{1,2}$/.test(l))) {
  problems.push(`x labels are not dates: ${JSON.stringify(xLabels.slice(0, 4))}`);
}
if (!marks) { problems.push('the chart drew axes but no data marks'); }

console.log(`  ${file}`);
console.log(`    grid ${grid}  zero ${zero}  marks ${marks}`);
console.log(`    y: ${yLabels.join('  ')}`);
console.log(`    x: ${xLabels.join('  ')}`);
if (problems.length) {
  console.error('\nFAILED:');
  problems.forEach(p => console.error(`  - ${p}`));
  process.exit(1);
}
console.log('    the chart drew axes, grid, dated labels and data marks');
