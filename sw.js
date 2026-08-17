// Empire State Trail Companion — service worker (offline support)
// v8: the shell is fetched past the browser's own HTTP cache now — see the fetch
// handler. Bumping the name is what evicts whatever that cache had already put in here.
const CACHE='est-shell-v8';
const RUNTIME='est-runtime-v2';
const SHELL=['./','./index.html','./est-core.js','./broadsheet/styles.css','./manifest.json','./icon-192.png','./icon-512.png'];

self.addEventListener('install',e=>{
  e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting()));
});
self.addEventListener('activate',e=>{
  e.waitUntil(caches.keys().then(ks=>Promise.all(
    ks.filter(k=>k!==CACHE && k!==RUNTIME).map(k=>caches.delete(k))
  )).then(()=>self.clients.claim()));
});
self.addEventListener('fetch',e=>{
  const req=e.request;
  if(req.method!=='GET') return;
  const url=new URL(req.url);
  // App shell (same origin): network-first, falling back to cache when offline.
  //
  // This used to be cache-first (`caches.match(req).then(r => r || fetch(req))`),
  // which never revalidated: once index.html and est-core.js were in the cache
  // they were served forever and no shipped change could reach an existing
  // install. Bumping CACHE only helped once the browser happened to notice a new
  // sw.js. The shell is small and same-origin, so paying a network round-trip for
  // freshness is the right trade — offline still works via the cache fallback.
  if(url.origin===location.origin){
    /* And past the browser's OWN cache, for the three files the app is made of. Being
       network-first was not enough: GitHub Pages serves these with `cache-control:
       max-age=600`, and a plain fetch() is answered from the HTTP cache without ever
       reaching the network — so for ten minutes after a deploy a phone that already had
       the app kept running the previous build while the new one sat on the server, and
       the plan it loaded from the account had fields that build did not understand.
       Only the shell: everything else here is data files that are big, change rarely, and
       have every right to be cached the ordinary way. */
    const shell=/(^|\/)(index\.html|est-core\.js|styles\.css)$/.test(url.pathname)
      || url.pathname.endsWith('/');
    let go;
    try{ go=fetch(shell ? new Request(req,{cache:'reload'}) : req); }
    catch(err){ go=fetch(req); }   // a navigate-mode Request can refuse to be rebuilt
    e.respondWith(
      go.then(res=>{
        if(res && res.status===200){ const copy=res.clone(); caches.open(CACHE).then(c=>c.put(req,copy)); }
        return res;
      }).catch(()=> caches.match(req).then(r=> r || caches.match('./index.html')))
    );
    return;
  }
  // Cross-origin the app depends on — the Leaflet library and the Source Serif
  // webfont as well as tiles and ArcGIS data. Without the first two cached, going
  // offline costs you the map library itself, not just the imagery.
  // Serve cached if present, refresh in the background.
  if(/tile\.openstreetmap\.org|server\.arcgisonline\.com|services\.arcgis\.com|cdnjs\.cloudflare\.com|fonts\.googleapis\.com|fonts\.gstatic\.com/.test(url.host)){
    e.respondWith(caches.open(RUNTIME).then(async c=>{
      const cached=await c.match(req);
      const network=fetch(req).then(res=>{ if(res && res.status===200) c.put(req,res.clone()); return res; }).catch(()=>cached);
      return cached || network;
    }));
  }
});
