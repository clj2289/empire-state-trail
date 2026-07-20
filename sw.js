// Empire State Trail Companion — service worker (offline support)
const CACHE='est-shell-v5';
const RUNTIME='est-runtime-v2';
const SHELL=['./','./index.html','./est-core.js','./broadsheet/styles.css','./manifest.json','./icon-192.png','./icon-512.png',
  './gpx/Total_Shoreline_Trail_via_West_River.gpx'];

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
    e.respondWith(
      fetch(req).then(res=>{
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
