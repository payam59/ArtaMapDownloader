#!/usr/bin/env python

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import threading

from urllib.parse import urlparse
from urllib.parse import parse_qs
import email
from email.message import Message
import argparse
import time
import json
import os
import re
import sys
import socket
import base64
import mimetypes
import uuid

from file_writer import FileWriter
from mbtiles_writer import MbtilesWriter
from repo_writer import RepoWriter
from utils import Utils
from tile_index import TileIndex, Region, IndexRegistry, openTileSet, listTileSets, validatePattern, validateRelativeDirectory, isInside, DEFAULT_PATTERN
from job_runner import DownloadJob

try:
    from stitch import Stitcher
    STITCH_AVAILABLE = True
except ImportError:
    STITCH_AVAILABLE = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
UI_DIR = os.path.join(BASE_DIR, "UI")

lock = threading.Lock()

stitch_job = {"state": "idle", "message": "", "files": []}
stitch_job_lock = threading.Lock()

registry = IndexRegistry()
current_job = None
current_job_lock = threading.Lock()

allowed_hosts = set()

def makeEmptyPng():
	# fully transparent 256px tile for "nothing here" (generated, so its pixels are known)
	from PIL import Image
	import io
	output = io.BytesIO()
	Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(output, "PNG")
	return output.getvalue()

EMPTY_PNG = makeEmptyPng()


def run_stitch(output_dir, min_zoom, max_zoom):
    try:
        files = []
        for zoom in range(min_zoom, max_zoom + 1):
            with stitch_job_lock:
                stitch_job["state"] = "stitching"
                stitch_job["message"] = f"Stitching zoom level {zoom}..."

            path = Stitcher.stitch_zoom_level(output_dir, zoom)
            if path:
                files.append(path)

        with stitch_job_lock:
            stitch_job["state"] = "done"
            stitch_job["message"] = f"Stitched {len(files)} zoom level(s)"
            stitch_job["files"] = files

    except Exception as e:
        with stitch_job_lock:
            stitch_job["state"] = "error"
            stitch_job["message"] = str(e)


def _parse_header(line):
    msg = Message()
    msg['content-type'] = line
    params = msg.get_params()
    if not params:
        return line, {}
    main = params[0][0]
    pdict = {k: v for k, v in params[1:]}
    return main, pdict


def _parse_multipart(fp, pdict):
    boundary = pdict['boundary']
    if isinstance(boundary, bytes):
        boundary = boundary.decode('utf-8')
    length = pdict.get('CONTENT-LENGTH', -1)
    body = fp.read(length) if isinstance(length, int) and length >= 0 else fp.read()
    msg = email.message_from_bytes(
        f'Content-Type: multipart/form-data; boundary={boundary}\r\n\r\n'.encode() + body
    )
    result = {}
    for part in msg.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        name = part.get_param('name', header='content-disposition')
        if name:
            payload = part.get_payload(decode=True)
            if payload is not None:
                try:
                    value = payload.decode('utf-8')
                except UnicodeDecodeError:
                    value = payload.decode('latin-1')
                result.setdefault(name, []).append(value)
    return result


def resolveTypedPath(path):
	# a path typed in the UI: absolute, existing, no NUL; '..' is resolved before anything is read
	if not isinstance(path, str) or path.strip() == "" or "\x00" in path:
		raise ValueError("Enter a path")
	path = path.strip().strip('"')
	if not os.path.isabs(path):
		raise ValueError("Enter an absolute path, or pick a folder from the list")
	resolved = os.path.realpath(path)
	if not os.path.exists(resolved):
		raise ValueError("Path does not exist: " + path)
	return resolved


class serverHandler(BaseHTTPRequestHandler):

	protocol_version = "HTTP/1.1"

	def log_message(self, format, *args):
		# per-request logging to the console slowed big downloads down; only log errors
		if len(args) > 1 and str(args[1]).startswith(("4", "5")):
			BaseHTTPRequestHandler.log_message(self, format, *args)

	def randomString(self):
		return uuid.uuid4().hex.upper()[0:6]

	def writerByType(self, type):
		if(type == "mbtiles"):
			return MbtilesWriter
		elif(type == "repo"):
			return RepoWriter
		elif(type == "directory"):
			return FileWriter

	def sendBody(self, code, contentType, body, extraHeaders=None):
		self.send_response(code)
		self.send_header("Content-Type", contentType)
		self.send_header("Content-Length", str(len(body)))
		for key, value in (extraHeaders or {}).items():
			self.send_header(key, value)
		self.end_headers()
		if self.command != "HEAD":
			self.wfile.write(body)

	def sendJson(self, result, code=200):
		self.sendBody(code, "application/json", json.dumps(result).encode('utf-8'), {"Cache-Control": "no-store"})

	def hostAllowed(self):
		# DNS-rebinding guard: when bound to loopback only answer to loopback host names
		if not allowed_hosts:
			return True
		host = (self.headers.get("Host") or "").strip().lower()
		if host.startswith("["):
			host = host[:host.find("]") + 1]
		else:
			host = host.split(":")[0]
		return host in allowed_hosts

	def readJsonBody(self):
		length = int(self.headers.get('Content-Length') or 0)
		if length > 4 * 1024 * 1024:
			raise ValueError("Request too large")
		body = self.rfile.read(length) if length > 0 else b"{}"
		return json.loads(body.decode("utf-8") or "{}")

	def do_POST(self):

		if not self.hostAllowed():
			self.sendJson({"code": 403, "message": "Host not allowed"}, 403)
			return

		parts = urlparse(self.path)

		if parts.path.startswith("/api/"):
			# JSON API: requiring application/json means a cross-site page cannot post without a CORS preflight
			if not (self.headers.get('Content-Type') or "").startswith("application/json"):
				self.sendJson({"code": 415, "message": "Expected application/json"}, 415)
				return
			try:
				data = self.readJsonBody()
				result = self.handleApiPost(parts.path, data)
				self.sendJson(result, 200 if result.get("code", 200) == 200 else 400)
			except (ValueError, KeyError, TypeError) as e:
				self.sendJson({"code": 400, "message": str(e)}, 400)
			return

		ctype, pdict = _parse_header(self.headers.get('Content-Type'))
		pdict['boundary'] = bytes(pdict['boundary'], "utf-8")

		content_len = int(self.headers.get('Content-length'))
		pdict['CONTENT-LENGTH'] = content_len

		postvars = _parse_multipart(self.rfile, pdict)

		if parts.path == '/download-tile':
			# legacy per-tile endpoint, kept for compatibility. The UI now uses /api/job/*.

			x = int(postvars['x'][0])
			y = int(postvars['y'][0])
			z = int(postvars['z'][0])
			quad = str(postvars['quad'][0])
			timestamp = int(postvars['timestamp'][0])
			outputDirectory = str(postvars['outputDirectory'][0])
			outputFile = str(postvars['outputFile'][0])
			outputType = str(postvars['outputType'][0])
			outputScale = int(postvars['outputScale'][0])
			source = str(postvars['source'][0])
			preview = postvars.get('preview', ['0'])[0] == '1'

			replaceMap = {
				"x": str(x),
				"y": str(y),
				"z": str(z),
				"quad": quad,
				"timestamp": str(timestamp),
			}

			for key, value in replaceMap.items():
				newKey = str("{" + str(key) + "}")
				outputDirectory = outputDirectory.replace(newKey, value)
				outputFile = outputFile.replace(newKey, value)

			result = {}

			filePath = os.path.join(OUTPUT_DIR, validateRelativeDirectory(outputDirectory), validatePattern(outputFile))

			if self.writerByType(outputType).exists(filePath, x, y, z):
				result["code"] = 200
				result["message"] = 'Tile already exists'

			else:

				code, data = Utils.fetchTileScaled(source, x, y, z, outputScale)
				result["code"] = code

				if code == 200:
					self.writerByType(outputType).ensureDirectory(lock, os.path.dirname(filePath))
					self.writerByType(outputType).addTileData(lock, filePath, data, x, y, z, outputScale)

					# the base64 copy of every tile is what made the browser grow; only send it on request
					if preview:
						result["image"] = base64.b64encode(data).decode("utf-8")

					result["message"] = 'Tile Downloaded'

				else:
					result["message"] = 'Download failed'

			self.sendJson(result)
			return

		elif parts.path == '/start-download':
			outputType = str(postvars['outputType'][0])
			outputScale = int(postvars['outputScale'][0])
			outputDirectory = str(postvars['outputDirectory'][0])
			outputFile = str(postvars['outputFile'][0])
			minZoom = int(postvars['minZoom'][0])
			maxZoom = int(postvars['maxZoom'][0])
			timestamp = int(postvars['timestamp'][0])
			bounds = str(postvars['bounds'][0])
			boundsArray = list(map(float, bounds.split(",")))
			center = str(postvars['center'][0])
			centerArray = list(map(float, center.split(",")))

			outputDirectory = validateRelativeDirectory(outputDirectory.replace("{timestamp}", str(timestamp)))
			outputFile = outputFile.replace("{timestamp}", str(timestamp))

			filePath = os.path.join(OUTPUT_DIR, outputDirectory, outputFile)

			self.writerByType(outputType).addMetadata(lock, os.path.join(OUTPUT_DIR, outputDirectory), filePath, outputFile, "Map Tiles Downloader via AliFlux", "png", boundsArray, centerArray, minZoom, maxZoom, "mercator", 256 * outputScale)

			self.sendJson({"code": 200, "message": 'Metadata written'})
			return

		elif parts.path == '/end-download':
			outputType = str(postvars['outputType'][0])
			outputDirectory = str(postvars['outputDirectory'][0])
			outputFile = str(postvars['outputFile'][0])
			minZoom = int(postvars['minZoom'][0])
			maxZoom = int(postvars['maxZoom'][0])
			timestamp = int(postvars['timestamp'][0])

			outputDirectory = validateRelativeDirectory(outputDirectory.replace("{timestamp}", str(timestamp)))
			outputFile = outputFile.replace("{timestamp}", str(timestamp))

			filePath = os.path.join(OUTPUT_DIR, outputDirectory, outputFile)

			self.writerByType(outputType).close(lock, os.path.join(OUTPUT_DIR, outputDirectory), filePath, minZoom, maxZoom)

			self.sendJson({"code": 200, "message": 'Downloaded ended'})
			return

		elif parts.path == '/stitch-tiles':
			if not STITCH_AVAILABLE:
				self.sendJson({"code": 500, "message": "pyvips not installed. Run: pip install pyvips[binary]"})
				return

			minZoom = int(postvars['minZoom'][0])
			maxZoom = int(postvars['maxZoom'][0])

			if 'folder' in postvars:
				# a folder opened in the resume panel
				full_output_dir = resolveTypedPath(str(postvars['folder'][0]))
			else:
				outputDirectory = str(postvars['outputDirectory'][0])
				timestamp = int(postvars['timestamp'][0])
				outputDirectory = outputDirectory.replace('{timestamp}', str(timestamp))
				full_output_dir = os.path.join(OUTPUT_DIR, validateRelativeDirectory(outputDirectory))

			with stitch_job_lock:
				stitch_job["state"] = "starting"
				stitch_job["message"] = "Starting..."
				stitch_job["files"] = []

			t = threading.Thread(target=run_stitch, args=(full_output_dir, minZoom, maxZoom), daemon=True)
			t.start()

			self.sendJson({"code": 200, "message": "Stitching started"})
			return

		self.sendJson({"code": 404, "message": "Not found"}, 404)

	def handleApiPost(self, path, data):
		global current_job

		if path == '/api/open':
			if data.get("name"):
				name = str(data["name"])
				if "/" in name or "\\" in name or name in (".", ".."):
					raise ValueError("Bad folder name")
				target = os.path.join(OUTPUT_DIR, name)
				if data.get("file"):
					target = os.path.join(target, validatePattern(str(data["file"])))
				if not isInside(OUTPUT_DIR, target) or not os.path.exists(target):
					raise ValueError("Folder not found")
			else:
				target = resolveTypedPath(data.get("path"))
			pattern = data.get("pattern") or None
			if pattern is not None:
				pattern = validatePattern(str(pattern))
			index = openTileSet(target, pattern, bool(data.get("verify")))
			registry.add(index)
			result = index.describe()
			result["code"] = 200
			return result

		elif path == '/api/stats':
			index = registry.get(str(data.get("ds")))
			if index is None:
				raise ValueError("Tile set is not open any more, open it again")
			region = Region(data["bounds"], data.get("polygon")) if data.get("bounds") else index.region
			minZoom = int(data.get("minZoom", index.minZoom or 0))
			maxZoom = int(data.get("maxZoom", index.maxZoom or 0))
			minZoom, maxZoom = min(minZoom, maxZoom), max(minZoom, maxZoom)
			if minZoom < 0 or maxZoom > 24:
				raise ValueError("Zoom must be between 0 and 24")
			started = time.time()
			result = index.stats(region, minZoom, maxZoom)
			result["ms"] = round((time.time() - started) * 1000, 1)
			result["code"] = 200
			return result

		elif path == '/api/job/start':
			return self.startJob(data)

		elif path == '/api/job/stop':
			with current_job_lock:
				job = current_job
			if job is not None:
				job.stop()
			return {"code": 200, "message": "Stopping"}

		raise ValueError("Unknown endpoint")

	def startJob(self, data):
		global current_job

		with current_job_lock:
			if current_job is not None and current_job.isActive():
				raise ValueError("A download is already running")

			mode = data.get("mode", "new")
			source = str(data.get("source") or "").strip()
			outputScale = int(data.get("outputScale", 1))
			threads = int(data.get("threads", 4))
			minZoom = int(data.get("minZoom", 0))
			maxZoom = int(data.get("maxZoom", 0))
			minZoom, maxZoom = min(minZoom, maxZoom), max(minZoom, maxZoom)

			if outputScale not in (1, 2):
				raise ValueError("Output scale must be 1 or 2")
			if minZoom < 0 or maxZoom > 24:
				raise ValueError("Zoom must be between 0 and 24")
			if mode == "failed" and source == "":
				known = registry.get(str(data.get("ds")))
				source = known.source if known is not None else ""
			if not re.search(r"\{(x|y|z|quad)\}", source):
				raise ValueError("Map tile source must contain {x}, {y}, {z} or {quad}")
			if not source.lower().startswith(("http://", "https://")):
				raise ValueError("Map tile source must be an http(s) URL")

			metadataArgs = None

			if mode == "new":
				timestamp = int(data.get("timestamp") or int(time.time() * 1000))
				outputType = str(data.get("outputType", "directory"))
				if outputType not in ("directory", "mbtiles", "repo"):
					raise ValueError("Unknown output type")
				outputDirectory = validateRelativeDirectory(str(data.get("outputDirectory", "{timestamp}")).replace("{timestamp}", str(timestamp)))
				outputFile = validatePattern(str(data.get("outputFile", DEFAULT_PATTERN)).replace("{timestamp}", str(timestamp)))
				folder = os.path.join(OUTPUT_DIR, outputDirectory)
				if not isInside(OUTPUT_DIR, folder):
					raise ValueError("Output directory must be inside the output folder")
				os.makedirs(folder, exist_ok=True)
				if outputType == "directory":
					index = TileIndex("directory", folder, outputFile)
				else:
					index = TileIndex(outputType, os.path.join(folder, *outputFile.split("/")))
					os.makedirs(os.path.dirname(index.path), exist_ok=True)
				# a fixed directory name that already holds tiles is completed, never duplicated
				index.scan()
				index.loadManifest()
				index.timestamp = timestamp
				region = Region(data["bounds"], data.get("polygon"))
			else:
				index = registry.get(str(data.get("ds")))
				if index is None:
					raise ValueError("Tile set is not open any more, open it again")
				region = Region(data["bounds"], data.get("polygon")) if data.get("bounds") else index.region
				if region is None:
					raise ValueError("No region: draw a rectangle first")

			index.source = source
			index.outputScale = outputScale
			registry.add(index)

			w, s, e, n = region.bounds
			outputName = index.pattern if index.kind == "directory" else os.path.basename(index.path)
			metadataArgs = (index.folder, index.path, outputName, "Map Tiles Downloader via AliFlux", "png", region.bounds, [(w + e) / 2, (s + n) / 2, maxZoom], minZoom, maxZoom, "mercator", 256 * outputScale)

			job = DownloadJob(index, source, region, minZoom, maxZoom, outputScale, threads, "failed" if mode == "failed" else "missing", metadataArgs)
			current_job = job
			job.start()

		result = {"code": 200, "message": "Started", "ds": index.token}
		result.update(index.describe())
		return result

	def do_HEAD(self):
		self.do_GET()

	def do_GET(self):

		if not self.hostAllowed():
			self.sendJson({"code": 403, "message": "Host not allowed"}, 403)
			return

		parts = urlparse(self.path)
		query = parse_qs(parts.query)

		if parts.path == '/stitch-status':
			with stitch_job_lock:
				result = dict(stitch_job)
			result["code"] = 200
			self.sendJson(result)
			return

		if parts.path == '/api/datasets':
			self.sendJson({"code": 200, "outputDirectory": OUTPUT_DIR, "items": listTileSets(OUTPUT_DIR)})
			return

		if parts.path == '/api/job/status':
			with current_job_lock:
				job = current_job
			if job is None:
				self.sendJson({"code": 200, "state": "idle"})
				return
			since = int(query.get("since", ["0"])[0])
			result = job.status(since)
			result["code"] = 200
			self.sendJson(result)
			return

		match = re.fullmatch(r"/api/(tiles|coverage)/(\d+)/(\d+)/(\d+)(\.png)?", parts.path)
		if match:
			kind = match.group(1)
			z, x, y = int(match.group(2)), int(match.group(3)), int(match.group(4))
			index = registry.get(query.get("ds", [""])[0])
			if index is None or z > 24 or x >= (1 << z) or y >= (1 << z):
				self.sendBody(404, "text/plain", b"not found")
				return

			if kind == "tiles":
				data = index.readTile(x, y, z)
				if data is None:
					# transparent instead of 404 so the map does not log an error per missing tile
					self.sendBody(200, "image/png", EMPTY_PNG, {"Cache-Control": "no-store"})
				else:
					self.sendBody(200, Utils.sniffImageType(data), data, {"Cache-Control": "no-store"})
				return

			dz = int(query.get("dz", [str(index.maxZoom or z)])[0])
			region = index.region
			if "bounds" in query:
				region = Region([float(v) for v in query["bounds"][0].split(",")])
			data = index.renderCoverage(z, x, y, dz, region) if 0 <= dz <= 24 else None
			self.sendBody(200, "image/png", data or EMPTY_PNG, {"Cache-Control": "no-store"})
			return

		path = parts.path.strip('/')
		if path == "":
			path = "index.htm"

		file = os.path.join(UI_DIR, path)

		# the old code served any file reachable with '..' in the path
		if not isInside(UI_DIR, file) or not os.path.isfile(file):
			self.sendBody(404, "text/plain", b"not found")
			return

		mime = mimetypes.MimeTypes().guess_type(file)[0] or "application/octet-stream"

		with open(file, "rb") as f:
			self.sendBody(200, mime, f.read())

class serverThreadedHandler(ThreadingHTTPServer):
	"""Handle requests in a separate thread."""
	daemon_threads = True
	# on Windows SO_REUSEADDR lets a second server silently share the port
	allow_reuse_address = sys.platform != "win32"

def run():
	parser = argparse.ArgumentParser(description="Map Tiles Downloader")
	parser.add_argument("--port", type=int, default=8080, help="port to listen on (default 8080)")
	parser.add_argument("--host", default="127.0.0.1", help="address to bind (default 127.0.0.1; use 0.0.0.0 inside Docker)")
	args = parser.parse_args()

	print('Starting Server...')
	os.makedirs(OUTPUT_DIR, exist_ok=True)

	if args.host in ("127.0.0.1", "localhost", "::1"):
		allowed_hosts.update(["127.0.0.1", "localhost", "[::1]"])

	server_address = (args.host, args.port)
	httpd = serverThreadedHandler(server_address, serverHandler)
	print('Running Server...')

	print("Open http://localhost:" + str(args.port) + "/ to view the application.")

	try:
		httpd.serve_forever()
	except KeyboardInterrupt:
		pass

if __name__ == "__main__":
	run()
