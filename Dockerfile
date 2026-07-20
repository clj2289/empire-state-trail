# Empire State Trail Companion — static PWA served by nginx.
#
# The app is plain HTML/CSS/JS with no build step, so this is a single stage:
# copy the shell in and configure caching. Map tiles, the Leaflet library, the
# Source Serif webfont and the NY State ArcGIS facility data are all fetched
# from the public internet at runtime, so the container needs outbound network
# on first load. After that the service worker caches them.
FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copied explicitly rather than `COPY . .` so nothing outside the app shell
# (git history, CI config, scratch files) can end up in the image.
COPY index.html est-core.js manifest.json sw.js /usr/share/nginx/html/
COPY broadsheet/ /usr/share/nginx/html/broadsheet/
COPY icon-192.png icon-512.png icon-maskable-512.png /usr/share/nginx/html/

EXPOSE 80

# 127.0.0.1 rather than localhost, so the check does not depend on how the
# container resolves localhost across IPv4/IPv6.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1
