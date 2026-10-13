#!/usr/bin/env python

import urllib.request
import urllib.error
import urllib.parse
import http.client
import threading
import uuid
import random
import time
import ssl
import socket
import os
import io
import math

from PIL import Image

# Tile extensions recognised when scanning a folder of existing tiles
TILE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")

# monkey patching SSL certificate issue
# DONT use it in a prod/sensitive environment
SSL_CONTEXT = ssl._create_unverified_context()

HEADERS = {
	'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
	'Referer': 'https://www.google.com/maps/',
	'Accept': 'image/avif,image/webp,image/png,image/*,*/*;q=0.8',
	'Accept-Language': 'en-US,en;q=0.9',
}

DOWNLOAD_TIMEOUT = 20
DOWNLOAD_RETRIES = 3
RETRY_AFTER_CAP = 60
RETRYABLE_CODES = (408, 429, 500, 502, 503, 504)

# keep-alive connections are per thread; a configured HTTP(S)_PROXY is only honoured by urllib
_threadLocal = threading.local()
USE_URLLIB = bool(urllib.request.getproxies())

class Utils:

	@staticmethod
	def randomString():
		return uuid.uuid4().hex.upper()[0:6]

	def getChildTiles(x, y, z):
		childX = x * 2
		childY = y * 2
		childZ = z + 1

		return [
			(childX, childY, childZ),
			(childX+1, childY, childZ),
			(childX+1, childY+1, childZ),
			(childX, childY+1, childZ),
		]

	def makeQuadKey(tile_x, tile_y, level):
		quadkey = ""
		for i in range(level):
			bit = level - i
			digit = ord('0')
			mask = 1 << (bit - 1)  # if (bit - 1) > 0 else 1 >> (bit - 1)
			if (tile_x & mask) != 0:
				digit += 1
			if (tile_y & mask) != 0:
				digit += 2
			quadkey += chr(digit)
		return quadkey

	@staticmethod
	def quadKeyToTile(quadkey):
		x = 0
		y = 0
		z = len(quadkey)
		for i in range(z, 0, -1):
			mask = 1 << (i - 1)
			digit = quadkey[z - i]
			if digit == '1':
				x |= mask
			elif digit == '2':
				y |= mask
			elif digit == '3':
				x |= mask
				y |= mask
			elif digit != '0':
				raise ValueError("bad quadkey")
		return x, y, z

	@staticmethod
	def num2deg(xtile, ytile, zoom):
		n = 2.0 ** zoom
		lon_deg = xtile / n * 360.0 - 180.0
		lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * ytile / n)))
		lat_deg = math.degrees(lat_rad)
		return (lat_deg, lon_deg)

	@staticmethod
	def long2tile(lon, zoom):
		# same formula as the UI, clamped to the valid tile range
		n = 2 ** zoom
		return min(n - 1, max(0, int(math.floor((lon + 180.0) / 360.0 * n))))

	@staticmethod
	def lat2tile(lat, zoom):
		n = 2 ** zoom
		lat = max(-85.0511287798, min(85.0511287798, lat))
		rad = math.radians(lat)
		value = (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * n
		return min(n - 1, max(0, int(math.floor(value))))

	@staticmethod
	def qualifyURL(url, x, y, z):

		scale22 = 23 - (z * 2)

		replaceMap = {
			"x": str(x),
			"y": str(y),
			"z": str(z),
			"scale:22": str(scale22),
			"quad": Utils.makeQuadKey(x, y, z),
		}

		for key, value in replaceMap.items():
			newKey = str("{" + str(key) + "}")
			url = url.replace(newKey, value)

		return url

	@staticmethod
	def mergeQuadTile(quadTiles):

		width = 0
		height = 0

		for tile in quadTiles:
			if(tile is not None):
				width = quadTiles[0].size[0] * 2
				height = quadTiles[1].size[1] * 2
				break

		if width == 0 or height == 0:
			return None

		canvas = Image.new('RGB', (width, height))

		if quadTiles[0] is not None:
			canvas.paste(quadTiles[0], box=(0,0))

		if quadTiles[1] is not None:
			canvas.paste(quadTiles[1], box=(width - quadTiles[1].size[0], 0))

		if quadTiles[2] is not None:
			canvas.paste(quadTiles[2], box=(width - quadTiles[2].size[0], height - quadTiles[2].size[1]))

		if quadTiles[3] is not None:
			canvas.paste(quadTiles[3], box=(0, height - quadTiles[3].size[1]))

		return canvas

	@staticmethod
	def parseRetryAfter(value):
		if value is None:
			return None
		try:
			return max(0.0, min(RETRY_AFTER_CAP, float(value)))
		except ValueError:
			return None

	@staticmethod
	def getConnection(scheme, netloc, fresh=False):
		# one keep-alive connection per worker thread and host: a new TCP (and TLS) connection per tile
		# was slow and, at high rates, ran Windows out of ephemeral ports
		connections = getattr(_threadLocal, "connections", None)
		if connections is None:
			connections = {}
			_threadLocal.connections = connections
		key = (scheme, netloc)
		connection = connections.get(key)
		if connection is not None and fresh:
			connection.close()
			connection = None
		if connection is None:
			if scheme == "https":
				connection = http.client.HTTPSConnection(netloc, timeout=DOWNLOAD_TIMEOUT, context=SSL_CONTEXT)
			else:
				connection = http.client.HTTPConnection(netloc, timeout=DOWNLOAD_TIMEOUT)
			connections[key] = connection
		return connection

	@staticmethod
	def dropConnection(scheme, netloc):
		connections = getattr(_threadLocal, "connections", None) or {}
		connection = connections.pop((scheme, netloc), None)
		if connection is not None:
			connection.close()

	@staticmethod
	def requestOnce(url):
		# returns (status, body, retryAfter); raises OSError / HTTPException on network failure
		parts = urllib.parse.urlsplit(url)

		if USE_URLLIB or parts.scheme not in ("http", "https"):
			try:
				req = urllib.request.Request(url, headers=HEADERS)
				with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT, context=SSL_CONTEXT) as response:
					return response.status, response.read(), None
			except urllib.error.HTTPError as e:
				retryAfter = e.headers.get("Retry-After") if e.headers else None
				e.close()
				return e.code, None, retryAfter

		path = parts.path or "/"
		if parts.query:
			path += "?" + parts.query

		for attempt in range(2):
			connection = Utils.getConnection(parts.scheme, parts.netloc, fresh=attempt > 0)
			try:
				connection.request("GET", path, headers=HEADERS)
				response = connection.getresponse()
				body = response.read()
			except (http.client.HTTPException, ConnectionError) as e:
				# a kept-alive connection the server already closed: retry once on a fresh one
				Utils.dropConnection(parts.scheme, parts.netloc)
				if attempt == 0:
					continue
				raise
			except OSError:
				Utils.dropConnection(parts.scheme, parts.netloc)
				raise

			if (response.getheader("Connection") or "").lower() == "close":
				Utils.dropConnection(parts.scheme, parts.netloc)

			if response.status in (301, 302, 303, 307, 308):
				# rare for tile servers; let urllib follow the redirect
				try:
					req = urllib.request.Request(url, headers=HEADERS)
					with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT, context=SSL_CONTEXT) as redirected:
						return redirected.status, redirected.read(), None
				except urllib.error.HTTPError as e:
					retryAfter = e.headers.get("Retry-After") if e.headers else None
					e.close()
					return e.code, None, retryAfter

			return response.status, body, response.getheader("Retry-After")

	@staticmethod
	def fetchTile(url, x, y, z, stopEvent=None, onThrottle=None):
		# Returns (code, bytes or None). Retries 429/5xx/network errors with backoff and honours Retry-After.

		url = Utils.qualifyURL(url, x, y, z)

		code = 0
		for attempt in range(DOWNLOAD_RETRIES + 1):

			if stopEvent is not None and stopEvent.is_set():
				return -2, None

			retryAfter = None
			try:
				status, data, retryAfterHeader = Utils.requestOnce(url)
				if status == 200:
					if not data:
						code = -3
					else:
						return 200, data
				else:
					code = status
					retryAfter = Utils.parseRetryAfter(retryAfterHeader)
			except (urllib.error.URLError, http.client.HTTPException, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
				print("NETWORK ERROR: " + url + " " + str(e))
				code = -1

			if code not in RETRYABLE_CODES and code not in (-1, -3):
				return code, None

			if attempt == DOWNLOAD_RETRIES:
				break

			delay = (2 ** attempt) + random.random()
			if retryAfter is not None:
				delay = max(delay, retryAfter)
				if onThrottle is not None:
					onThrottle(retryAfter)

			if stopEvent is not None:
				if stopEvent.wait(delay):
					return -2, None
			else:
				time.sleep(delay)

		return code, None

	@staticmethod
	def fetchTileScaled(url, x, y, z, outputScale, stopEvent=None, onThrottle=None):

		if outputScale == 1:
			return Utils.fetchTile(url, x, y, z, stopEvent, onThrottle)

		elif outputScale == 2:

			childImages = []
			try:
				for childX, childY, childZ in Utils.getChildTiles(x, y, z):

					code, data = Utils.fetchTile(url, childX, childY, childZ, stopEvent, onThrottle)
					if code != 200:
						return code, None

					image = Image.open(io.BytesIO(data))
					image.load()
					childImages.append(image)

				canvas = Utils.mergeQuadTile(childImages)
				if canvas is None:
					return -1, None

				with canvas:
					output = io.BytesIO()
					canvas.save(output, "PNG")
					return 200, output.getvalue()

			finally:
				for image in childImages:
					image.close()

		#TODO implement custom scale
		return -1, None

	@staticmethod
	def downloadFile(url, destination, x, y, z):
		# kept for compatibility: download a tile into destination atomically

		code, data = Utils.fetchTile(url, x, y, z)
		if code == 200:
			Utils.writeFileAtomic(destination, data)
		return code

	@staticmethod
	def downloadFileScaled(url, destination, x, y, z, outputScale):

		code, data = Utils.fetchTileScaled(url, x, y, z, outputScale)
		if code == 200:
			Utils.writeFileAtomic(destination, data)
		return code

	@staticmethod
	def writeFileAtomic(destination, data):
		# write to a temp name in the same directory, then rename, so a killed run never leaves a truncated tile
		tempPath = destination + ".part-" + Utils.randomString()
		try:
			with open(tempPath, "wb") as f:
				f.write(data)
			for attempt in range(3):
				try:
					os.replace(tempPath, destination)
					return
				except PermissionError:
					# Windows: target briefly open by a reader
					if attempt == 2:
						raise
					time.sleep(0.05)
		finally:
			if os.path.exists(tempPath):
				try:
					os.remove(tempPath)
				except OSError:
					pass

	@staticmethod
	def sniffImageType(data):
		if data[:8] == b'\x89PNG\r\n\x1a\n':
			return "image/png"
		if data[:3] == b'\xff\xd8\xff':
			return "image/jpeg"
		if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
			return "image/webp"
		return "application/octet-stream"
