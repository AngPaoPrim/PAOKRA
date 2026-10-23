const fs=require('fs'),crypto=require('crypto'),assert=require('assert'),path=require('path');
const root=__dirname;
const html=fs.readFileSync(path.join(root,'app/web/index.html'),'utf8');
const guide=fs.readFileSync(path.join(root,'app/web/usage-guide.js'),'utf8');
const css=fs.readFileSync(path.join(root,'app/web/usage-guide.css'),'utf8');
const ui=fs.readFileSync(path.join(root,'app/web/paokra-ui.js'),'utf8');
const pkg=require(path.join(root,'desktop/package.json'));
const lock=require(path.join(root,'desktop/package-lock.json'));
const tests=[];
function test(name,fn){fn();tests.push(name);console.log('PASS: '+name)}
function sha(file,alg='sha256',encoding='hex'){return crypto.createHash(alg).update(fs.readFileSync(file)).digest(encoding)}
test('guide stylesheet and script are linked exactly once',()=>{assert.equal((html.match(/usage-guide\.css/g)||[]).length,1);assert.equal((html.match(/usage-guide\.js/g)||[]).length,1)});
test('guide startup preference and Settings reopen entry points remain',()=>{for(const term of ['paokra.guide.suppress.v1','pkGuideShowStartup','data-open-guide','function openAfterBoot()','if(get(SUPPRESS_KEY)!=="1")openAfterBoot()'])assert(guide.includes(term),term)});
test('guide still covers core features and troubleshooting',()=>{for(const term of ['Clip Flow','AI Seam / Make Frames','Virtual Camera','แก้ปัญหาเบื้องต้น','Settings & อัปเดต','OOMSIN','PRELOAD MISS (cold decoder)'])assert(guide.includes(term),term)});
test('guide modal remains accessible, tabbed, dismissible, keyboard-closeable',()=>{for(const term of ['role="dialog"','aria-modal="true"','role="tablist"','data-guide-close','e.key==="Escape"'])assert(guide.includes(term),term)});
test('Halloween background preset still selects the Halloween profile',()=>{assert(ui.includes('id:"halloween"'));assert(ui.includes('bg:"halloween"'));assert(css.includes('html[data-bg=halloween] body')||css.includes('html[data-bg="halloween"] body'))});
test('animated orange-purple gradients, ambient aurora and pumpkin motion are present',()=>{for(const term of ['rgba(255,126,34','rgba(157,70,255','pkHalloweenGradientFlow','pkHalloweenAuroraDrift','pkHalloweenPumpkinBob'])assert(css.includes(term),term)});
test('reduced-motion and existing animation opt-out are respected',()=>{assert(css.includes('prefers-reduced-motion:reduce'));assert(fs.readFileSync(path.join(root,'app/web/style.css'),'utf8').includes('html[data-animations="off"] *'))});
test('desktop package and lockfile are aligned to stable 1.5.0',()=>{assert.equal(pkg.version,'1.5.0');assert.equal(lock.version,pkg.version);assert.equal(lock.packages[''].version,pkg.version)});
test('OOMSIN Easter egg remains in the guide',()=>{assert(guide.includes('OOMSIN'));assert(css.includes('pk-guide-oomsin'))});
const packaged=path.join(root,'dist/win-unpacked/resources/paokra/app/web/');
test('packaged guide assets match source files',()=>{for(const f of ['usage-guide.js','usage-guide.css']){assert(fs.existsSync(packaged+f),f+' missing from package');assert.equal(sha(path.join(root,'app/web/'+f)),sha(packaged+f),f+' differs from source')}const ph=fs.readFileSync(packaged+'index.html','utf8');assert(ph.includes('usage-guide.js'));assert(ph.includes('usage-guide.css'))});
test('latest.yml version, byte size and SHA-512 match the installer',()=>{const y=fs.readFileSync(path.join(root,'dist/latest.yml'),'utf8'),m=/version:\s*(.+)/.exec(y),s=/size:\s*(\d+)/.exec(y),h=/sha512:\s*(.+)/.exec(y),installer=path.join(root,'dist/PAOKRA-Studio-Setup.exe');assert(m&&m[1].trim()===pkg.version);assert(s&&Number(s[1])===fs.statSync(installer).size);assert(h&&h[1].trim()===sha(installer,'sha512','base64'))});
console.log('USAGE_GUIDE_150_QA='+tests.length+' PASS');
