#!/usr/bin/env node
/**
 * What is actually on the lot: query Avis's own availability service for a list of
 * counters and a date range, and write the fleet each one is offering.
 *
 * This answers the question the brand's own site refuses to — "where near here is there
 * a minivan" — because avis.com only ever asks it of one counter at a time.
 *
 * ---- why this is a browser and not a fetch --------------------------------------
 * The endpoint is plain and unauthenticated:
 *
 *   POST https://www.avis.com/web/reservation/vehicles?context.locale=en-US&...
 *   {"pickupLocation":"TLH","dropoffLocation":"TLH","pickupDate":"2026-09-10", ...}
 *
 * No API key, no bearer token. But it sits behind DataDome, which fingerprints the TLS
 * handshake and not just the cookie jar — curl carrying a freshly harvested `datadome`
 * cookie still gets a 403 and a captcha URL. Copying headers does not help; the
 * connection itself is what gives it away.
 *
 * So the request has to originate from a real browser. It does NOT have to be a page
 * load, though, and that is the whole trick: Chrome loads avis.com once, and every
 * query after that is a fetch() evaluated inside that page. One navigation, then N
 * cheap XHRs — seconds each rather than the half-minute a full reservation flow costs.
 *
 * Node rather than Python (which the rest of tools/ is written in) for one reason:
 * driving Chrome means speaking CDP, CDP is a WebSocket, and Node 24 has a WebSocket
 * client built in while Python's standard library has none. No packages either way.
 *
 * ---- usage -----------------------------------------------------------------------
 *   node tools/fetch_availability.mjs --codes TLH,AVTF0,T3F --from 2026-09-10 --days 3
 *   node tools/fetch_availability.mjs --near 30.39,-84.35 --radius 60 --from 2026-09-10
 *   node tools/fetch_availability.mjs --state fl --from 2026-09-10 --days 3,7
 *
 * --days takes a list, because rental length changes what is offered: a one-way that
 * comes up empty at three days can fill at seven. See the note in the README of the
 * board this feeds.
 */

import { spawn } from 'node:child_process';
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import os from 'node:os';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = dirname(HERE);

const CHROME = process.env.CHROME_BIN
  || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

// ---- arguments ---------------------------------------------------------------------
const argv = process.argv.slice(2);
const arg = (name, dflt) => {
  const i = argv.indexOf('--' + name);
  return i >= 0 && argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[i + 1] : dflt;
};
const has = name => argv.includes('--' + name);

const OPT = {
  codes: arg('codes', ''),
  state: arg('state', ''),
  near: arg('near', ''),
  radius: +arg('radius', 60),
  from: arg('from', ''),
  days: String(arg('days', '3')).split(',').map(Number).filter(n => n > 0),
  dropoff: arg('dropoff', ''),        // a code, for one-way; blank means round trip
  /* An AWD is the contract the rate hangs off, and it filters the fleet as well as the
     price — the State of Florida's is X00000, printed as "#X00000" on its portal, and
     the # is decoration. Run with and without it to see what the contract actually
     entitles you to, which is not a comparison avis.com will do for you. */
  awd: String(arg('awd', '')).replace(/^#/, ''),
  awdType: arg('awd-type', 'PARTNER'),   // COUPON | PARTNER | RATE | UNKNOWN
  time: arg('time', '10:00'),
  age: +arg('age', 25),
  limit: +arg('limit', 0),
  out: arg('out', join(ROOT, 'data', 'availability.json')),
  port: +arg('port', 9333),
  pause: +arg('pause', 900),
  headful: has('headful'),
};
if (!OPT.from) {
  console.error('need --from YYYY-MM-DD (see --help in the header comment)');
  process.exit(1);
}

// ---- which counters ----------------------------------------------------------------
function milesBetween(a, b, c, d) {
  const m = ((a + c) / 2) * Math.PI / 180;
  return Math.hypot((d - b) * Math.cos(m) * 69.17, (c - a) * 69.17);
}
function loadCounters() {
  if (OPT.codes) return OPT.codes.split(',').map(c => ({ c: c.trim().toUpperCase(), b: 'avis' }));
  // Otherwise take them from a rentals bundle produced by fetch_car_rentals.py.
  const file = OPT.state
    ? join(ROOT, 'data', `rentals-${OPT.state}.json`)
    : join(ROOT, 'data', 'rentals.json');
  if (!existsSync(file)) {
    console.error(`no counter list at ${file} — pass --codes, or run fetch_car_rentals.py first`);
    process.exit(1);
  }
  let recs = JSON.parse(readFileSync(file, 'utf8'));
  // Only Avis: this endpoint is Avis's. Budget has its own and the codes are not shared.
  recs = recs.filter(r => r.b === 'avis' && !r.r && !r.z);
  if (OPT.near) {
    const [la, lo] = OPT.near.split(',').map(Number);
    recs = recs.map(r => ({ ...r, _d: milesBetween(la, lo, r.y, r.x) }))
      .filter(r => r._d <= OPT.radius)
      .sort((a, b) => a._d - b._d);
  }
  return recs;
}

// ---- chrome + CDP ------------------------------------------------------------------
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function startChrome() {
  const profile = join(os.tmpdir(), 'est-avail-profile-' + process.pid);
  mkdirSync(profile, { recursive: true });
  const args = [
    OPT.headful ? '--headless=false' : '--headless=new',
    '--disable-gpu', '--no-sandbox', '--no-first-run', '--no-default-browser-check',
    '--disable-background-networking', '--window-size=1400,2000',
    `--remote-debugging-port=${OPT.port}`, `--user-data-dir=${profile}`,
    `--user-agent=${UA}`, 'about:blank',
  ].filter(a => a !== '--headless=false' || OPT.headful);
  const proc = spawn(CHROME, args, { stdio: 'ignore', detached: false });
  for (let i = 0; i < 40; i++) {
    await sleep(500);
    try {
      const r = await fetch(`http://127.0.0.1:${OPT.port}/json/version`);
      if (r.ok) return proc;
    } catch { /* not up yet */ }
  }
  throw new Error('Chrome did not open a debugging port');
}

async function connect() {
  const targets = await (await fetch(`http://127.0.0.1:${OPT.port}/json`)).json();
  const page = targets.find(t => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  ws.addEventListener('message', ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m.result); pending.delete(m.id); }
  });
  await new Promise(r => ws.addEventListener('open', r));
  const send = (method, params = {}, ms = 90000) => {
    const n = ++id;
    ws.send(JSON.stringify({ id: n, method, params }));
    return new Promise(res => {
      const t = setTimeout(() => { pending.delete(n); res({ __timeout: true }); }, ms);
      pending.set(n, v => { clearTimeout(t); res(v); });
    });
  };
  await send('Page.enable'); await send('Runtime.enable');
  return { ws, send };
}

/* An evaluate that survives the page reloading under it — accepting the cookie banner
   does exactly that, and a bare call across the gap comes back empty for reasons that
   have nothing to do with the expression. */
async function evalx(send, expr, tries = 3, ms = 90000) {
  for (let i = 0; i < tries; i++) {
    const r = await send('Runtime.evaluate',
      { expression: expr, returnByValue: true, awaitPromise: true }, ms);
    if (!r.__timeout && !r.exceptionDetails && r.result && r.result.value !== undefined)
      return r.result.value;
    if (r.exceptionDetails) {
      const d = (r.exceptionDetails.exception || {}).description || '';
      if (i === tries - 1) console.error('   eval error:', d.slice(0, 160));
    }
    await sleep(2000);
  }
  return null;
}

/* The cookie wall sits over everything and swallows clicks, but it is only in the way
   for the one navigation we do — after that every query is a fetch, not a click. It
   still has to go, because until it is dismissed the page has not finished setting the
   session cookies the endpoint checks. */
async function warmUp(send) {
  await send('Page.navigate', { url: 'https://www.avis.com/en/reservation/make-reservation' });
  await sleep(14000);
  const sel = await evalx(send, `(() => {
    const b=[...document.querySelectorAll('button')]
      .find(x=>/^(Agree|Allow All)$/i.test((x.innerText||'').trim()));
    if(!b) return null; b.setAttribute('data-warm','1');
    const r=b.getBoundingClientRect();
    return JSON.stringify({x:r.x+r.width/2, y:r.y+r.height/2});
  })()`);
  if (sel) {
    const p = JSON.parse(sel);
    await send('Input.dispatchMouseEvent', { type: 'mouseMoved', ...p });
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', ...p, button: 'left', clickCount: 1 });
    await send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...p, button: 'left', clickCount: 1 });
    await sleep(4000);
  }
  return await evalx(send, 'location.host');
}

// ---- one query ---------------------------------------------------------------------
const addDays = (ymd, n) => {
  const d = new Date(ymd + 'T12:00:00');
  d.setDate(d.getDate() + n);
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
    + '-' + String(d.getDate()).padStart(2, '0');
};

async function query(send, pick, drop, from, to) {
  const body = {
    pickupLocation: pick, dropoffLocation: drop || pick,
    pickupDate: from, pickupTime: OPT.time,
    dropoffDate: to, dropoffTime: OPT.time,
    age: OPT.age, priceView: 'LOWEST_PRICE',
    /* The BFF rejects {code,type:'AWD'} outright and names the schema it wants:
       type must be one of COUPON, PARTNER, RATE, UNKNOWN, and the code goes in `value`.
       An AWD is the partner contract, so PARTNER is the one. */
    discountCodes: OPT.awd ? [{ type: OPT.awdType, value: OPT.awd }] : [],
    countryOfResidence: 'US', domain: 'www.avis.com',
  };
  const expr = `(async () => {
    const q = new URLSearchParams({
      "context.locale":"en-US","context.domainCountry":"US","context.domain":"www.avis.com",
      "context.correlationIdentifier": crypto.randomUUID(), device:"DESKTOP"});
    try{
      const r = await fetch("/web/reservation/vehicles?"+q, {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: ${JSON.stringify(JSON.stringify(body))} });
      const t = await r.text();
      let j = null; try { j = JSON.parse(t); } catch {}
      return JSON.stringify({ status: r.status,
        vehicles: (j && j.vehicles) ? j.vehicles.map(v => ({
          sipp: v.sippCode, type: v.vehicleType, desc: v.description,
          make: v.makeName, code: v.vehicleCode, avail: v.vehicleAvailability,
          feat: (v.features||[]).reduce((a,f)=>(a[f.name]=f.value,a),{})
        })) : null,
        err: (r.ok && j) ? null : t.slice(0,900) });
    }catch(e){ return JSON.stringify({status:-1, vehicles:null, err:String(e).slice(0,180)}); }
  })()`;
  const raw = await evalx(send, expr);
  return raw ? JSON.parse(raw) : { status: -2, vehicles: null, err: 'evaluate failed' };
}

// ---- run ----------------------------------------------------------------------------
const counters = loadCounters();
const list = OPT.limit ? counters.slice(0, OPT.limit) : counters;
console.error(`${list.length} counters × ${OPT.days.length} rental length(s) = `
  + `${list.length * OPT.days.length} queries`);

const chrome = await startChrome();
const { ws, send } = await connect();
const host = await warmUp(send);
console.error('warmed up on', host);

const out = [];
let blocked = 0;
for (const site of list) {
  for (const nDays of OPT.days) {
    const to = addDays(OPT.from, nDays);
    const res = await query(send, site.c, OPT.dropoff, OPT.from, to);
    if (res.status === 403 || res.status === 429) {
      blocked++;
      console.error(`  ${site.c} ${nDays}d -> ${res.status} blocked; re-warming`);
      await warmUp(send);
      const again = await query(send, site.c, OPT.dropoff, OPT.from, to);
      if (again.vehicles) { res.status = again.status; res.vehicles = again.vehicles; }
    }
    const v = res.vehicles || [];
    const avail = v.filter(x => x.avail === 'AVAILABLE');
    out.push({
      code: site.c, brand: 'avis', name: site.n || '', addr: site.a || '',
      lat: site.y ?? null, lng: site.x ?? null,
      miles: site._d != null ? +site._d.toFixed(1) : null,
      from: OPT.from, to, days: nDays, dropoff: OPT.dropoff || site.c, awd: OPT.awd || null,
      status: res.status, error: res.err || null,
      vehicles: v,
    });
    console.error(`  ${site.c.padEnd(6)} ${String(nDays).padStart(2)}d  `
      + `[${res.status}] ${String(avail.length).padStart(2)} available / ${v.length} listed`
      + (res.err ? '  ' + res.err.slice(0, 60) : ''));
    await sleep(OPT.pause);
  }
}

mkdirSync(dirname(OPT.out), { recursive: true });
writeFileSync(OPT.out, JSON.stringify(out, null, 0) + '\n');
const withCars = out.filter(o => (o.vehicles || []).some(v => v.avail === 'AVAILABLE'));
console.error(`\n${out.length} results -> ${OPT.out}`);
console.error(`${withCars.length} counter/length pairs had something available`
  + (blocked ? `, ${blocked} were blocked and retried` : ''));

ws.close();
try { chrome.kill(); } catch { /* already gone */ }
process.exit(0);
