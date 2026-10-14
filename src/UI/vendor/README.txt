Vendored third-party files
==========================

These files are copied byte for byte from their upstream sources so the app works
without an internet connection. Do not edit them. They are NOT covered by the
Arta Map Downloader licence: each keeps its own licence, listed below, with the
full licence text in the LICENSES folder.

Mapbox GL JS v0.50.0: BSD-3-Clause, LICENSES/mapbox-gl-js-v0.50.0-LICENSE.txt
  (the licence file also carries the notices for code it includes, such as glfx.js)
Mapbox GL Draw v1.0.9: ISC, LICENSES/mapbox-gl-draw-v1.0.9-LICENSE.txt

Downloaded 2026-10-13. SHA-256 of each file as stored here:

mapbox/mapbox-gl.js
  version: Mapbox GL JS v0.50.0    licence: BSD-3-Clause    size: 680440 bytes
  source:  https://api.mapbox.com/mapbox-gl-js/v0.50.0/mapbox-gl.js
  sha256:  d48c2418b73eb6611591133014b8bbdc2303a328022c76dc46d7f6f6d1abdee1

mapbox/mapbox-gl.css
  version: Mapbox GL JS v0.50.0    licence: BSD-3-Clause    size: 30570 bytes
  source:  https://api.mapbox.com/mapbox-gl-js/v0.50.0/mapbox-gl.css
  sha256:  72887a4771f289848b01b324e25648af9b7142adc175b3749ca9755388a2a0d5

mapbox/mapbox-gl-draw.js
  version: Mapbox GL Draw v1.0.9    licence: ISC    size: 111676 bytes
  source:  https://api.mapbox.com/mapbox-gl-js/plugins/mapbox-gl-draw/v1.0.9/mapbox-gl-draw.js
  sha256:  87661a0dc0b139ff5ad13b18065deebb58fb9a8ae8ad59d322b9628e5ea88cba

mapbox/mapbox-gl-draw.css
  version: Mapbox GL Draw v1.0.9    licence: ISC    size: 32904 bytes
  source:  https://api.mapbox.com/mapbox-gl-js/plugins/mapbox-gl-draw/v1.0.9/mapbox-gl-draw.css
  sha256:  33f5f05c3ccbdf27bcb22ff87cdfc3f11c43ff0ab4cbf3399e5397b1101c66f7

LICENSES/mapbox-gl-js-v0.50.0-LICENSE.txt
  source:  https://github.com/mapbox/mapbox-gl-js/blob/v0.50.0/LICENSE.txt    size: 4452 bytes
  sha256:  4826e9bca5913c3bea8ea06bf1d4f9a043fe8a02034eb52090981ca9b9f7b2b2

LICENSES/mapbox-gl-draw-v1.0.9-LICENSE.txt
  source:  https://github.com/mapbox/mapbox-gl-draw/blob/v1.0.9/LICENSE    size: 741 bytes
  sha256:  eaceee727f9c40c30afbc701f30b9f1a92f65c2a868c89f3cab9af5e6b00a366

To check a file: certutil -hashfile <file> SHA256 (Windows) or sha256sum <file>.
The app never sets a Mapbox access token, so the library's Mapbox telemetry
(events.mapbox.com) never runs; it only does so with a token and a mapbox.com tile URL.
