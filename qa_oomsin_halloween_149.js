const fs=require('fs'),assert=require('assert'),crypto=require('crypto');
const js=fs.readFileSync('app/web/usage-guide.js','utf8');
const css=fs.readFileSync('app/web/usage-guide.css','utf8');
const html=fs.readFileSync('app/web/index.html','utf8');
const packaged='dist/win-unpacked/resources/paokra/app/web/';
function sha(p){return crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex')}
assert(js.includes('OOMSIN'),'OOMSIN credit missing');
assert(js.includes('pk-guide-oomsin'),'Easter egg hook missing');
assert(css.includes('.pk-guide-oomsin'),'Easter egg styling missing');
assert(css.includes('Halloween polish'),'Halloween guide polish missing');
assert(html.includes('usage-guide.js')&&html.includes('usage-guide.css'),'Guide assets not linked');
for(const f of ['usage-guide.js','usage-guide.css']){assert(fs.existsSync(packaged+f),f+' missing from packaged runtime');assert.strictEqual(sha('app/web/'+f),sha(packaged+f),f+' packaged hash mismatch')}
console.log('PASS: OOMSIN Easter egg exists');
console.log('PASS: Halloween polish styles exist');
console.log('PASS: guide assets are linked and match packaged runtime');
console.log('OOMSIN_HALLOWEEN_149_QA=3 PASS');
