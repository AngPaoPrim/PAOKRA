const fs=require('fs'),crypto=require('crypto'),assert=require('assert'),path=require('path');
const root=__dirname;
const html=fs.readFileSync(path.join(root,'app/web/index.html'),'utf8');
const guide=fs.readFileSync(path.join(root,'app/web/usage-guide.js'),'utf8');
const guideCss=fs.readFileSync(path.join(root,'app/web/usage-guide.css'),'utf8');
const uiCss=fs.readFileSync(path.join(root,'app/web/paokra-ui.css'),'utf8');
const uiJs=fs.readFileSync(path.join(root,'app/web/paokra-ui.js'),'utf8');
const pkg=require(path.join(root,'desktop/package.json')),lock=require(path.join(root,'desktop/package-lock.json'));
const tests=[];
function test(name,fn){fn();tests.push(name);console.log('PASS: '+name)}
function sha(file,alg='sha256',encoding='hex'){return crypto.createHash(alg).update(fs.readFileSync(file)).digest(encoding)}
test('guide stylesheet and script are linked exactly once',()=>{assert.equal((html.match(/usage-guide\.css/g)||[]).length,1);assert.equal((html.match(/usage-guide\.js/g)||[]).length,1)});
test('guide startup suppression preference and Settings reopen remain',()=>{for(const term of ['paokra.guide.suppress.v1','pkGuideShowStartup','data-open-guide','function openAfterBoot()','if(get(SUPPRESS_KEY)!=="1")openAfterBoot()'])assert(guide.includes(term),term)});
test('guide still covers features and troubleshooting',()=>{for(const term of ['Clip Flow','AI Seam / Make Frames','Virtual Camera','แก้ปัญหาเบื้องต้น','Settings & อัปเดต','OOMSIN','PRELOAD MISS (cold decoder)'])assert(guide.includes(term),term)});
test('guide modal remains accessible, tabbed and dismissible',()=>{for(const term of ['role="dialog"','aria-modal="true"','role="tablist"','data-guide-close','e.key==="Escape"'])assert(guide.includes(term),term)});
test('Halloween migration and preset remain in the app runtime',()=>{assert(uiJs.includes('if(previousUIVersion<3)'));assert(uiCss.includes('pkHalloweenMainGradient'))});
test('package and lockfile consistently target stable 1.5.1',()=>{assert.equal(pkg.version,'1.5.1');assert.equal(lock.version,pkg.version);assert.equal(lock.packages[''].version,pkg.version)});
test('OOMSIN Easter egg remains intact',()=>{assert(guide.includes('OOMSIN'));assert(guideCss.includes('pk-guide-oomsin'))});
const packaged=path.join(root,'dist/win-unpacked/resources/paokra/app/web/');
test('main and guide CSS/JS are included and match the packaged app',()=>{for(const f of ['paokra-ui.css','paokra-ui.js','usage-guide.css','usage-guide.js']){assert(fs.existsSync(packaged+f),f+' missing');assert.equal(sha(path.join(root,'app/web/'+f)),sha(packaged+f),f+' differs from source')}});
test('latest.yml version, size and SHA-512 match the newly built installer',()=>{const y=fs.readFileSync(path.join(root,'dist/latest.yml'),'utf8'),m=/version:\s*(.+)/.exec(y),s=/size:\s*(\d+)/.exec(y),h=/sha512:\s*(.+)/.exec(y),f=path.join(root,'dist/PAOKRA-Studio-Setup.exe');assert(m&&m[1].trim()===pkg.version);assert(s&&Number(s[1])===fs.statSync(f).size);assert(h&&h[1].trim()===sha(f,'sha512','base64'))});
console.log('USAGE_GUIDE_151_QA='+tests.length+' PASS');
