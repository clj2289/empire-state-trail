// Empire State Trail Companion — service worker (offline support)
const CACHE='est-shell-v7';
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
/* Serve what we have, refresh behind you. Used for everything that is big, slow to
   fetch and not the app's own code — where a round trip before first paint buys
   nothing that a background update cannot deliver just as well one use later. */
function staleWhileRevalidate(cacheName, req){
  return caches.open(cacheName).then(async c=>{
    const cached=await c.match(req);
    const network=fetch(req).then(res=>{ if(res && res.status===200) c.put(req,res.clone()); return res; }).catch(()=>cached);
    return cached || network;
  });
}
self.addEventListener('fetch',e=>{
  const req=e.request;
  if(req.method!=='GET') return;
  const url=new URL(req.url);
  /* Bundled data: same origin, but emphatically not shell. pois-nearby.json is
     1.6 MB of OpenStreetMap snapshot that the app now fetches only when a rider
     switches an OSM layer on, and the network-first rule below would put them back
     on the wire for it in every new session. It is a build-time snapshot that is
     already months behind live OSM, so a blocking round trip buys freshness nobody
     can perceive — a background refresh lands the next regeneration soon enough.
     Matched on '/data/<name>.json' rather than an absolute path because the app is
     served from a subdirectory on GitHub Pages and from the root under nginx.
     Kept in RUNTIME, not the shell cache: bumping the shell version to ship a code
     change should not also throw away a megabyte and a half of data. */
  if(url.origin===location.origin && /\/data\/[^/]+\.json$/.test(url.pathname)){
    e.respondWith(staleWhileRevalidate(RUNTIME, req));
    return;
  }
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
    e.respondWith(staleWhileRevalidate(RUNTIME, req));
  }
});
