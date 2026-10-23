const fs = require('fs');
const assert = require('assert');
const path = require('path');

const root = __dirname;
const cssPath = path.join(root, 'app', 'web', 'usage-guide.css');
const jsPath = path.join(root, 'app', 'web', 'usage-guide.js');
const htmlPath = path.join(root, 'app', 'web', 'index.html');
const packagePath = path.join(root, 'desktop', 'package.json');

const css = fs.readFileSync(cssPath, 'utf8');
const js = fs.readFileSync(jsPath, 'utf8');
const html = fs.readFileSync(htmlPath, 'utf8');
const pkg = JSON.parse(fs.readFileSync(packagePath, 'utf8'));

assert.strictEqual(pkg.version, '1.5.0', 'desktop package version must be 1.5.0');
assert(css.includes('html[data-bg="halloween"] body'), 'Halloween body selector missing');
assert(css.includes('html[data-bg="halloween"] .main'), 'Main background selector missing');
assert(css.includes('html[data-bg="halloween"] .pkb-aur'), 'Aurora layer selector missing');
assert(css.includes('pkHalloweenGradientFlow'), 'Animated gradient keyframes missing');
assert(css.includes('pkHalloweenAuroraDrift'), 'Aurora motion keyframes missing');
assert(css.includes('pkHalloweenPumpkinBob'), 'Floating pumpkin animation missing');
assert(css.includes('prefers-reduced-motion:reduce'), 'Reduced-motion fallback missing');
assert(css.includes('data-animations="off"') || fs.readFileSync(path.join(root, 'app', 'web', 'style.css'), 'utf8').includes('html[data-animations="off"] *'), 'Animation opt-out style missing');
assert((html.match(/usage-guide\.css/g) || []).length === 1, 'Guide CSS link must exist exactly once');
assert((html.match(/usage-guide\.js/g) || []).length === 1, 'Guide JS link must exist exactly once');
assert(js.includes('pkGuideDialog'), 'Existing guide behavior unexpectedly missing');
assert(css.includes('pk-guide-oomsin'), 'Existing OOMSIN guide styling unexpectedly missing');
console.log('HALLOWEEN_MOTION_150_QA=12 PASS');
