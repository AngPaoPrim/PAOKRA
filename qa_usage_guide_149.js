const fs=require("fs"),crypto=require("crypto"),assert=require("assert"),path=require("path");
const html=fs.readFileSync("app/web/index.html","utf8");
const guide=fs.readFileSync("app/web/usage-guide.js","utf8");
const css=fs.readFileSync("app/web/usage-guide.css","utf8");
const ui=fs.readFileSync("app/web/paokra-ui.js","utf8");
const pkg=require("./desktop/package.json"),lock=require("./desktop/package-lock.json");
const tests=[];
function test(name,fn){fn();tests.push(name);console.log("PASS: "+name)}
function sha(file,alg="sha256",encoding="hex"){return crypto.createHash(alg).update(fs.readFileSync(file)).digest(encoding)}
test("guide stylesheet and script are linked exactly once",()=>{assert.equal((html.match(/usage-guide\.css/g)||[]).length,1);assert.equal((html.match(/usage-guide\.js/g)||[]).length,1)});
test("guide has startup suppression preference and settings re-open entry points",()=>{assert(guide.includes("paokra.guide.suppress.v1"));assert(guide.includes("pkGuideShowStartup"));assert(guide.includes("data-open-guide"));assert(guide.includes("function openAfterBoot()"));assert(guide.includes('window.__pkBootDone===true&&!$("#pkBoot")'));assert(guide.includes('if(get(SUPPRESS_KEY)!=="1")openAfterBoot()'))});
test("guide covers every requested functional area and troubleshooting",()=>{for(const term of ["Clip Flow","AI Seam / Make Frames","Virtual Camera","แก้ปัญหาเบื้องต้น","Settings & อัปเดต","OOMSIN","PRELOAD MISS (cold decoder)"])assert(guide.includes(term),term)});
test("modal is accessible, tabbed, dismissible and keyboard-closeable",()=>{for(const term of ['role="dialog"','aria-modal="true"','role="tablist"','data-guide-close','e.key==="Escape"'])assert(guide.includes(term),term)});
test("Halloween preset uses orange accent and Halloween background",()=>{assert(ui.includes('id:"halloween"'));assert(ui.includes('bg:"halloween"'));assert(css.includes('html[data-bg=halloween] body'))});
test("package metadata versions are consistent",()=>{assert.equal(pkg.version,"1.4.9-user-guide-halloween");assert.equal(lock.version,pkg.version);assert.equal(lock.packages[""].version,pkg.version)});
test("guide defaults to showing each launch until user suppresses it",()=>{assert(guide.includes("function openAfterBoot()"));assert(guide.includes('if(get(SUPPRESS_KEY)!=="1")openAfterBoot()'));assert(guide.includes("window.__pkBootDone===true"));assert(guide.includes('if(done||(check&&check.checked))set(SUPPRESS_KEY,"1")'))});
const packaged="dist/win-unpacked/resources/paokra/app/web/";
test("new guide assets are included in the packaged runtime and match source",()=>{for(const f of ["usage-guide.js","usage-guide.css"]){assert(fs.existsSync(packaged+f),f+" missing from package");assert.equal(sha("app/web/"+f),sha(packaged+f),f+" differs")};const ph=fs.readFileSync(packaged+"index.html","utf8");assert(ph.includes("usage-guide.js"));assert(ph.includes("usage-guide.css"))});
test("installer latest.yml version, byte size and SHA-512 match the generated installer",()=>{const y=fs.readFileSync("dist/latest.yml","utf8"),m=/version:\s*(.+)/.exec(y),s=/size:\s*(\d+)/.exec(y),h=/sha512:\s*(.+)/.exec(y);const f="dist/PAOKRA-Studio-Setup.exe";assert(m&&m[1].trim()===pkg.version);assert(s&&Number(s[1])===fs.statSync(f).size);assert(h&&h[1].trim()===sha(f,"sha512","base64"))});
console.log("USAGE_GUIDE_149_QA="+tests.length+" PASS");
