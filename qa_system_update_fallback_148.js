const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { EventEmitter } = require("events");
const assert = require("assert");
const source = fs.readFileSync(path.join(__dirname, "desktop", "main.cjs"), "utf8");
const start = source.indexOf("function getGithubText(");
const end = source.indexOf("let systemUpdateBusy=false;", start);
assert.ok(start >= 0 && end > start, "updater fallback helpers must exist");
const helperSource = source.slice(start, end).replace('}catch(_){return null;}', '}catch(err){console.log("RELEASE_ERR",tag,err);return null;}') + "\nglobalThis.__qa = { getGithubText, decodeReleaseEntities, releaseNotesText, mapGithubReleaseApi, fallbackGithubReleaseList };\n";
function mockedHttps(urlTable) {
  return {
    get(input, options, callback) {
      const req = new EventEmitter();
      const u = new URL(input.toString());
      const key = u.protocol + "//" + u.host + u.pathname;
      const item = urlTable.get(key) || { status: 404, body: "not found" }; console.log("MOCK",key,item.status);
      process.nextTick(() => {
        const res = new EventEmitter();
        res.statusCode = item.status;
        res.headers = {};
        res.setEncoding = () => {};
        callback(res);
        process.nextTick(() => {
          if (item.status === 200) res.emit("data", item.body);
          res.emit("end");
        });
      });
      return req;
    }
  };
}
async function run() {
  const tag = "v1.4.8-smooth-seam-beta";
  const tag2 = "v1.4.7-seam-workflow-beta";
  const tagBroken = "v1.4.2-no-yml";
  const expectedSha512 = Buffer.from("known setup binary hash").toString("base64");
  const atom = "<?xml version=\"1.0\"?>\n<feed xmlns=\"http://www.w3.org/2005/Atom\">\n" +
    "<entry><id>" + tag + "</id><link rel=\"alternate\" href=\"https://github.com/AngPaoPrim/PAOKRA/releases/tag/" + tag + "\"/><title>PAOKRA v1.4.8 &amp; Smooth Seam</title><content type=\"html\">&lt;p&gt;Better &lt;strong&gt;preload&lt;/strong&gt;&lt;/p&gt;&lt;li&gt;Fallback fixes&lt;/li&gt;</content></entry>\n" +
    "<entry><id>" + tag2 + "</id><link rel=\"alternate\" href=\"https://github.com/AngPaoPrim/PAOKRA/releases/tag/" + tag2 + "\"/><title>PAOKRA v1.4.7</title><content type=\"html\">Known release</content></entry>\n" +
    "<entry><id>" + tagBroken + "</id><link rel=\"alternate\" href=\"https://github.com/AngPaoPrim/PAOKRA/releases/tag/" + tagBroken + "\"/><title>Broken listing</title><content type=\"html\">No YAML</content></entry>\n</feed>";
  const table = new Map();
  table.set("https://github.com/AngPaoPrim/PAOKRA/releases.atom", {status:200,body:atom});
  for (const [t, publishedAt] of [[tag,"2026-10-23T12:00:00.000Z"],[tag2,"2026-10-23T09:00:00.000Z"]]) {
    const yaml = "version: " + t + "\nfiles:\n  - url: PAOKRA-Studio-Setup.exe\n    sha512: " + expectedSha512 + "\n    size: 123\npath: PAOKRA-Studio-Setup.exe\nsha512: " + expectedSha512 + "\nreleaseDate: '" + publishedAt + "'\n";
    table.set("https://github.com/AngPaoPrim/PAOKRA/releases/download/"+encodeURIComponent(t)+"/latest.yml", {status:200,body:yaml});
  }
  table.set("https://github.com/AngPaoPrim/PAOKRA/releases/download/"+encodeURIComponent(tagBroken)+"/latest.yml", {status:404,body:"missing"});
  const Module = require("module");
  const originalLoad = Module._load;
  const sandbox = { URL, Promise, String, Number, Math, Error, Buffer, console, process, require(name) {
    return name === "https" ? mockedHttps(table) : originalLoad(name, module, false);
  }};
  vm.createContext(sandbox);
  vm.runInContext(helperSource, sandbox, {filename:"desktop/main.cjs#updater-fallback"});
  const testFeed = await sandbox.__qa.getGithubText("https://github.com/AngPaoPrim/PAOKRA/releases.atom"); console.log("FEEDLEN",testFeed.length,"ENTRYCOUNT",testFeed.split("<entry>").length-1,"TITLE",testFeed.includes("Smooth Seam"));
  const result = await sandbox.__qa.fallbackGithubReleaseList("GitHub HTTP 403");
  assert.equal(result.length, 2, "must return only releases with verifiable latest.yml");
  assert.equal(result[0].tag, tag, "must sort newest release first");
  assert.equal(result[0].size, 123);
  assert.equal(result[0].digest, "sha512:"+expectedSha512);
  assert.equal(result[0].prerelease, true);
  assert.match(result[0].notes, /Better preload/);
  assert.match(result[0].name, /Smooth Seam/);
  assert.equal(result[1].tag, tag2);
  console.log("PASS: API 403 fallback parses Atom and includes only releases with Setup size + SHA-512 metadata");
  const noYmlTable = new Map([["https://github.com/AngPaoPrim/PAOKRA/releases.atom",{status:200,body:atom}]]);
  const sandbox2 = { URL, Promise, String, Number, Math, Error, Buffer, console, process, require(name) {
    return name === "https" ? mockedHttps(noYmlTable) : originalLoad(name, module, false);
  }};
  vm.createContext(sandbox2);
  vm.runInContext(helperSource, sandbox2, {filename:"desktop/main.cjs#updater-fallback-empty"});
  let rejected = false;
  try { await sandbox2.__qa.fallbackGithubReleaseList("GitHub HTTP 403"); } catch(e) {
    rejected = true;
    assert.match(String(e.message), /found no release with a verifiable Setup\/latest\.yml asset/);
  }
  assert.ok(rejected, "must fail closed if no release has verifiable metadata");
  console.log("PASS: empty/unverifiable fallback refuses to return installable versions");
}
run().catch(error => { console.error(error.stack || error); process.exitCode = 1; });