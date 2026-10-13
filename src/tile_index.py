import os
import re
import io
import json
import math
import time
import uuid
import sqlite3
import threading
from array import array
from datetime import datetime, timezone

from PIL import Image, ImageDraw

from utils import Utils, TILE_EXTENSIONS
from mbtiles_writer import MbtilesWriter
from repo_writer import RepoWriter
from file_writer import FileWriter

DEFAULT_PATTERN = "{z}/{x}/{y}.png"
MANIFEST_NAME = "job.json"
MANIFEST_VERSION = 1
FAILED_LIST_CAP = 20000
MAX_ZOOM = 24

# coverage overlay colours, palette index -> RGBA
# (tiles on disk outside the region are "have" too: a lighter green, never counted as missing)
STATE_OUTSIDE = 0
STATE_MISSING = 1
STATE_PRESENT = 2
STATE_EXTRA = 3
PALETTE = [
	0, 0, 0, 0,
	235, 59, 90, 120,
	32, 191, 107, 95,
	32, 191, 107, 55,
]
TRANSLATE_OUTSIDE = bytes([STATE_OUTSIDE, STATE_EXTRA] + [0] * 254)
TRANSLATE_INSIDE = bytes([STATE_MISSING, STATE_PRESENT] + [0] * 254)


def nowIso():
	return datetime.now(timezone.utc).isoformat(timespec="seconds")


def isInside(base, path):
	base = os.path.normcase(os.path.realpath(base))
	path = os.path.normcase(os.path.realpath(path))
	try:
		return os.path.commonpath([base, path]) == base
	except ValueError:
		return False


def validatePattern(pattern):
	# the file pattern must stay inside the chosen folder: no absolute paths, drives, streams or '..'
	if not isinstance(pattern, str) or pattern.strip() == "":
		raise ValueError("Output file pattern is empty")
	normalized = pattern.replace("\\", "/")
	if normalized.startswith("/") or ":" in normalized or "\x00" in normalized:
		raise ValueError("Output file pattern must be a relative path")
	for part in normalized.split("/"):
		if part in ("", ".", ".."):
			raise ValueError("Output file pattern may not contain empty, '.' or '..' segments")
	return normalized


def validateRelativeDirectory(directory):
	normalized = (directory or "").replace("\\", "/").strip("/")
	if normalized == "":
		raise ValueError("Output directory is empty")
	if ":" in normalized or "\x00" in normalized:
		raise ValueError("Output directory must be relative to the output folder")
	for part in normalized.split("/"):
		if part in ("", ".", ".."):
			raise ValueError("Output directory may not contain empty, '.' or '..' segments")
	return normalized


def readJson(path):
	try:
		# utf-8-sig: a manifest re-saved by an editor or PowerShell may start with a BOM
		with open(path, "r", encoding="utf-8-sig") as f:
			return json.load(f)
	except (OSError, ValueError):
		return None


def writeJsonAtomic(path, data):
	Utils.writeFileAtomic(path, json.dumps(data, indent=1).encode("utf-8"))


def verifyTileFile(path, size):
	# cheap integrity check of a tile's tail, catches truncated files
	try:
		with open(path, "rb") as f:
			head = f.read(12)
			f.seek(max(0, size - 32))
			tail = f.read(32)
	except OSError:
		return False
	if head[:8] == b'\x89PNG\r\n\x1a\n':
		return tail.endswith(b'IEND\xaeB`\x82')
	if head[:3] == b'\xff\xd8\xff':
		return b'\xff\xd9' in tail
	if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
		return int.from_bytes(head[4:8], "little") + 8 <= size
	return size > 0


class Region:
	# A download region: a lng/lat bounding box plus an optional polygon ring.
	# An axis-aligned rectangle (what the UI draws) uses the arithmetic fast path.

	def __init__(self, bounds, polygon=None):
		w, s, e, n = [float(v) for v in bounds]
		self.bounds = [min(w, e), min(s, n), max(w, e), max(s, n)]
		self.polygon = None
		if polygon:
			ring = [(float(p[0]), float(p[1])) for p in polygon]
			if len(ring) >= 4 and not Region.isAxisAlignedRect(ring):
				self.polygon = ring
				xs = [p[0] for p in ring]
				ys = [p[1] for p in ring]
				self.bounds = [min(xs), min(ys), max(xs), max(ys)]

	@staticmethod
	def isAxisAlignedRect(ring):
		points = ring[:-1] if ring[0] == ring[-1] else ring
		return len(points) == 4 and len(set(p[0] for p in points)) == 2 and len(set(p[1] for p in points)) == 2

	@staticmethod
	def fromJson(data):
		if not data:
			return None
		return Region(data["bounds"], data.get("polygon"))

	def toJson(self):
		w, s, e, n = self.bounds
		ring = self.polygon or [[w, n], [e, n], [e, s], [w, s], [w, n]]
		return {"bounds": self.bounds, "polygon": [list(p) for p in ring]}

	def tileRange(self, z):
		w, s, e, n = self.bounds
		return (Utils.long2tile(w, z), Utils.lat2tile(n, z), Utils.long2tile(e, z), Utils.lat2tile(s, z))

	def rowIntervals(self, z, y):
		x0, y0, x1, y1 = self.tileRange(z)
		if y < y0 or y > y1:
			return []
		if self.polygon is None:
			return [(x0, x1)]
		intervals = []
		start = None
		for x in range(x0, x1 + 1):
			if self.tileIntersects(x, y, z):
				if start is None:
					start = x
			elif start is not None:
				intervals.append((start, x - 1))
				start = None
		if start is not None:
			intervals.append((start, x1))
		return intervals

	def count(self, z):
		x0, y0, x1, y1 = self.tileRange(z)
		if self.polygon is None:
			return (x1 - x0 + 1) * (y1 - y0 + 1)
		return sum(b - a + 1 for y in range(y0, y1 + 1) for a, b in self.rowIntervals(z, y))

	def tiles(self, z):
		x0, y0, x1, y1 = self.tileRange(z)
		for y in range(y0, y1 + 1):
			for a, b in self.rowIntervals(z, y):
				for x in range(a, b + 1):
					yield x, y

	def tileIntersects(self, x, y, z):
		north, west = Utils.num2deg(x, y, z)
		south, east = Utils.num2deg(x + 1, y + 1, z)
		ring = self.polygon
		for px, py in ring:
			if west <= px <= east and south <= py <= north:
				return True
		for cx, cy in ((west, north), (east, north), (east, south), (west, south)):
			if Region.pointInRing(cx, cy, ring):
				return True
		edges = [((west, north), (east, north)), ((east, north), (east, south)), ((east, south), (west, south)), ((west, south), (west, north))]
		for i in range(len(ring) - 1):
			for a, b in edges:
				if Region.segmentsIntersect(ring[i], ring[i + 1], a, b):
					return True
		return False

	@staticmethod
	def pointInRing(x, y, ring):
		inside = False
		j = len(ring) - 1
		for i in range(len(ring)):
			xi, yi = ring[i]
			xj, yj = ring[j]
			if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi:
				inside = not inside
			j = i
		return inside

	@staticmethod
	def segmentsIntersect(p1, p2, p3, p4):
		def orient(a, b, c):
			value = (b[1] - a[1]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[1] - b[1])
			return 0 if value == 0 else (1 if value > 0 else 2)
		def onSegment(a, b, c):
			return min(a[0], c[0]) <= b[0] <= max(a[0], c[0]) and min(a[1], c[1]) <= b[1] <= max(a[1], c[1])
		o1, o2, o3, o4 = orient(p1, p2, p3), orient(p1, p2, p4), orient(p3, p4, p1), orient(p3, p4, p2)
		if o1 != o2 and o3 != o4:
			return True
		return (o1 == 0 and onSegment(p1, p3, p2)) or (o2 == 0 and onSegment(p1, p4, p2)) or (o3 == 0 and onSegment(p3, p1, p4)) or (o4 == 0 and onSegment(p3, p2, p4))


class Grid:
	# Presence of tiles at one zoom level: one byte per tile over a growable bounding box.
	# 1 byte/tile keeps hundreds of thousands of tiles in a few hundred KB.

	def __init__(self, x0, y0, x1, y1):
		self.x0 = x0
		self.y0 = y0
		self.w = x1 - x0 + 1
		self.h = y1 - y0 + 1
		self.data = bytearray(self.w * self.h)
		self.count = 0

	def contains(self, x, y):
		return self.x0 <= x < self.x0 + self.w and self.y0 <= y < self.y0 + self.h

	def get(self, x, y):
		if not self.contains(x, y):
			return 0
		return self.data[(y - self.y0) * self.w + (x - self.x0)]

	def set(self, x, y):
		if not self.contains(x, y):
			self.ensure(x, y, x, y)
		i = (y - self.y0) * self.w + (x - self.x0)
		if self.data[i] == 0:
			self.data[i] = 1
			self.count += 1

	def ensure(self, x0, y0, x1, y1):
		nx0 = min(self.x0, x0)
		ny0 = min(self.y0, y0)
		nx1 = max(self.x0 + self.w - 1, x1)
		ny1 = max(self.y0 + self.h - 1, y1)
		if nx0 == self.x0 and ny0 == self.y0 and nx1 == self.x0 + self.w - 1 and ny1 == self.y0 + self.h - 1:
			return
		nw = nx1 - nx0 + 1
		nh = ny1 - ny0 + 1
		data = bytearray(nw * nh)
		for row in range(self.h):
			src = row * self.w
			dst = (row + self.y0 - ny0) * nw + (self.x0 - nx0)
			data[dst:dst + self.w] = self.data[src:src + self.w]
		self.x0, self.y0, self.w, self.h, self.data = nx0, ny0, nw, nh, data

	def countRow(self, y, a, b):
		if y < self.y0 or y >= self.y0 + self.h:
			return 0
		a = max(a, self.x0)
		b = min(b, self.x0 + self.w - 1)
		if b < a:
			return 0
		start = (y - self.y0) * self.w
		return self.data[start + a - self.x0:start + b - self.x0 + 1].count(1)

	def sampleRow(self, y, start, step, size):
		# presence values for x = start + i*step, i in [0, size)
		out = bytearray(size)
		if y < self.y0 or y >= self.y0 + self.h:
			return out
		gx1 = self.x0 + self.w - 1
		lo = 0 if start >= self.x0 else -((start - self.x0) // step)
		hi = min(size - 1, (gx1 - start) // step) if gx1 >= start else -1
		if hi < lo:
			return out
		rowStart = (y - self.y0) * self.w
		first = rowStart + start + lo * step - self.x0
		last = rowStart + start + hi * step - self.x0
		out[lo:hi + 1] = self.data[first:last + 1:step]
		return out

	def bounds(self):
		# tight bounds of the present tiles
		rows = [r for r in range(self.h) if 1 in self.data[r * self.w:(r + 1) * self.w]]
		if not rows:
			return None
		minX = self.w
		maxX = -1
		for r in rows:
			row = self.data[r * self.w:(r + 1) * self.w]
			minX = min(minX, row.index(1))
			maxX = max(maxX, self.w - 1 - row[::-1].index(1))
		return (self.x0 + minX, self.y0 + rows[0], self.x0 + maxX, self.y0 + rows[-1])


class TileIndex:
	# An opened tile set (a folder with a file pattern, or an mbtiles/repo file):
	# what is on disk per zoom, its manifest and the region to complete.

	def __init__(self, kind, path, pattern=None):
		self.token = uuid.uuid4().hex[:12]
		self.kind = kind
		self.path = os.path.realpath(path)
		self.folder = self.path if kind == "directory" else os.path.dirname(self.path)
		self.pattern = validatePattern(pattern or DEFAULT_PATTERN) if kind == "directory" else None
		self.grids = {}
		self.lock = threading.Lock()
		self.version = 0
		self.manifest = None
		self.metadata = None
		self.region = None
		self.minZoom = None
		self.maxZoom = None
		self.source = ""
		self.outputScale = 1
		self.timestamp = None
		self.inferred = False
		self.scanSeconds = 0
		self.ignoredFiles = 0
		self.invalidFiles = 0
		self.verify = False
		self.lastOpened = time.time()

	@property
	def manifestPath(self):
		if self.kind == "directory":
			return os.path.join(self.folder, MANIFEST_NAME)
		return self.path + "." + MANIFEST_NAME

	def writer(self):
		if self.kind == "mbtiles":
			return MbtilesWriter
		if self.kind == "repo":
			return RepoWriter
		return FileWriter

	# ---------- scanning ----------

	def patternRegex(self):
		parts = re.split(r"(\{z\}|\{x\}|\{y\}|\{quad\}|\{timestamp\})", self.pattern)
		regex = ""
		seen = set()
		names = {"{z}": "z", "{x}": "x", "{y}": "y", "{quad}": "quad", "{timestamp}": "ts"}
		for i, part in enumerate(parts):
			if part in names:
				name = names[part]
				if name in seen:
					regex += "(?P=" + name + ")"
				else:
					seen.add(name)
					regex += "(?P<" + name + ">[0-3]+)" if name == "quad" else "(?P<" + name + ">\\d+)"
			else:
				literal = part
				if i == len(parts) - 1:
					root, ext = os.path.splitext(literal)
					if ext.lower() in TILE_EXTENSIONS:
						regex += re.escape(root) + r"\.(?:png|jpg|jpeg|webp)"
						continue
				regex += re.escape(literal)
		return re.compile(regex, re.IGNORECASE)

	def scan(self, verify=None, cleanParts=False):
		# cleanParts removes temp files left by a killed run (only our own '.part-' names)
		if verify is None:
			verify = self.verify
		self.verify = verify
		started = time.time()
		perZoomX = {}
		perZoomY = {}
		self.ignoredFiles = 0
		self.invalidFiles = 0

		def add(z, x, y):
			if z > MAX_ZOOM:
				return
			n = 1 << z
			if x >= n or y >= n:
				return
			if z not in perZoomX:
				perZoomX[z] = array('I')
				perZoomY[z] = array('I')
			perZoomX[z].append(x)
			perZoomY[z].append(y)

		if self.kind == "directory":
			regex = self.patternRegex()
			depth = self.pattern.count("/")
			stack = [(self.folder, "", 0)]
			while stack:
				directory, prefix, level = stack.pop()
				try:
					entries = os.scandir(directory)
				except OSError:
					continue
				with entries:
					for entry in entries:
						rel = prefix + entry.name
						if entry.is_dir(follow_symlinks=False):
							if level < depth:
								stack.append((entry.path, rel + "/", level + 1))
							continue
						if level != depth:
							continue
						match = regex.fullmatch(rel)
						if ".part-" in entry.name:
							if cleanParts and re.fullmatch(r".+\.part-[0-9A-F]{6}", entry.name):
								try:
									os.remove(entry.path)
								except OSError:
									pass
							continue
						if match is None:
							self.ignoredFiles += 1
							continue
						try:
							size = entry.stat().st_size
						except OSError:
							continue
						if size == 0 or (verify and not verifyTileFile(entry.path, size)):
							self.invalidFiles += 1
							continue
						groups = match.groupdict()
						if "quad" in groups and groups["quad"] is not None:
							x, y, z = Utils.quadKeyToTile(groups["quad"])
						else:
							x, y, z = int(groups["x"]), int(groups["y"]), int(groups["z"])
						add(z, x, y)
		else:
			writer = self.writer()
			if os.path.exists(self.path):
				column = "tile_cropped_data" if self.kind == "repo" else "tile_data"
				connection, connectionLock = writer.getConnection(self.path)
				with connectionLock:
					try:
						c = connection.execute("SELECT zoom_level, tile_column, tile_row FROM tiles WHERE length(" + column + ") > 0")
						for z, x, tmsY in c:
							add(z, x, (1 << z) - tmsY - 1)
					except sqlite3.OperationalError:
						pass

		grids = {}
		for z in perZoomX:
			xs = perZoomX[z]
			ys = perZoomY[z]
			grid = Grid(min(xs), min(ys), max(xs), max(ys))
			for x, y in zip(xs, ys):
				grid.set(x, y)
			grids[z] = grid
		perZoomX.clear()
		perZoomY.clear()

		with self.lock:
			self.grids = grids
			self.version += 1
		self.scanSeconds = round(time.time() - started, 3)

	# ---------- manifest / inference ----------

	def loadManifest(self):
		self.manifest = readJson(self.manifestPath)
		if self.kind == "directory":
			self.metadata = readJson(os.path.join(self.folder, "metadata.json"))
		else:
			self.metadata = self.readMbtilesMetadata()

		manifest = self.manifest or {}
		self.source = manifest.get("source", "")
		self.outputScale = int(manifest.get("outputScale", 1))
		self.timestamp = manifest.get("timestamp")
		self.region = Region.fromJson(manifest.get("region")) if manifest.get("region") else None
		self.minZoom = manifest.get("minZoom")
		self.maxZoom = manifest.get("maxZoom")
		self.inferred = self.manifest is None

		zooms = sorted(z for z, g in self.grids.items() if g.count > 0)

		if self.region is None and self.metadata and self.metadata.get("bounds"):
			try:
				bounds = [float(v) for v in str(self.metadata["bounds"]).split(",")]
				if len(bounds) == 4:
					self.region = Region(bounds)
			except ValueError:
				pass
		if self.minZoom is None and self.metadata and self.metadata.get("minzoom") not in (None, ""):
			self.minZoom = int(float(self.metadata["minzoom"]))
			self.maxZoom = int(float(self.metadata["maxzoom"]))
		if self.minZoom is None and zooms:
			self.minZoom = zooms[0]
			self.maxZoom = zooms[-1]

		if self.region is None and zooms:
			# no manifest or metadata: the box around what is on disk at the most detailed zoom
			z = zooms[-1]
			x0, y0, x1, y1 = self.grids[z].bounds()
			north, west = Utils.num2deg(x0, y0, z)
			south, east = Utils.num2deg(x1 + 1, y1 + 1, z)
			# shrink a hair so the box does not spill into the neighbouring tiles
			epsilon = 1e-9
			self.region = Region([west + epsilon, south + epsilon, east - epsilon, north - epsilon])

	def readMbtilesMetadata(self):
		if not os.path.exists(self.path):
			return None
		connection, connectionLock = self.writer().getConnection(self.path)
		with connectionLock:
			try:
				return dict(connection.execute("SELECT name, value FROM metadata").fetchall())
			except sqlite3.OperationalError:
				return None

	def saveManifest(self, updates):
		manifest = dict(self.manifest or {})
		manifest.update(updates)
		manifest["version"] = MANIFEST_VERSION
		manifest.setdefault("created", nowIso())
		manifest["updated"] = nowIso()
		manifest["outputType"] = self.kind
		manifest["outputFile"] = self.pattern if self.kind == "directory" else os.path.basename(self.path)
		os.makedirs(self.folder, exist_ok=True)
		writeJsonAtomic(self.manifestPath, manifest)
		self.manifest = manifest
		return manifest

	# ---------- queries ----------

	def isPresent(self, x, y, z):
		grid = self.grids.get(z)
		return grid is not None and grid.get(x, y) == 1

	def markPresent(self, x, y, z):
		with self.lock:
			grid = self.grids.get(z)
			if grid is None:
				grid = Grid(x, y, x, y)
				self.grids[z] = grid
			grid.set(x, y)
			self.version += 1

	def prepareRegion(self, region, minZoom, maxZoom):
		# grow the grids up front so writes during a job never reallocate per tile
		with self.lock:
			for z in range(minZoom, maxZoom + 1):
				x0, y0, x1, y1 = region.tileRange(z)
				grid = self.grids.get(z)
				if grid is None:
					self.grids[z] = Grid(x0, y0, x1, y1)
				else:
					grid.ensure(x0, y0, x1, y1)

	def stats(self, region, minZoom, maxZoom):
		zooms = []
		totals = {"total": 0, "present": 0, "missing": 0}
		for z in range(minZoom, maxZoom + 1):
			grid = self.grids.get(z)
			total = region.count(z) if region else 0
			present = 0
			if grid is not None and region is not None and grid.count > 0:
				x0, y0, x1, y1 = region.tileRange(z)
				# only rows that can hold tiles on disk; a huge enlarged region costs nothing extra
				for y in range(max(y0, grid.y0), min(y1, grid.y0 + grid.h - 1) + 1):
					for a, b in region.rowIntervals(z, y):
						present += grid.countRow(y, a, b)
			zooms.append({"z": z, "total": total, "present": present, "missing": total - present, "onDisk": grid.count if grid else 0})
			totals["total"] += total
			totals["present"] += present
			totals["missing"] += total - present
		return {"zooms": zooms, "totals": totals}

	def zoomsOnDisk(self):
		return [{"z": z, "count": g.count} for z, g in sorted(self.grids.items()) if g.count > 0]

	def tilePath(self, x, y, z, ext=None):
		replaceMap = {
			"x": str(x),
			"y": str(y),
			"z": str(z),
			"quad": Utils.makeQuadKey(x, y, z),
			"timestamp": str(self.timestamp or 0),
		}
		rel = self.pattern
		for key, value in replaceMap.items():
			rel = rel.replace("{" + key + "}", value)
		if ext is not None:
			rel = os.path.splitext(rel)[0] + ext
		return os.path.join(self.folder, *rel.split("/"))

	def readTile(self, x, y, z):
		if self.kind == "directory":
			path = self.tilePath(x, y, z)
			candidates = [path] + [os.path.splitext(path)[0] + ext for ext in TILE_EXTENSIONS if not path.lower().endswith(ext)]
			for candidate in candidates:
				try:
					with open(candidate, "rb") as f:
						data = f.read()
					if data:
						return data
				except OSError:
					continue
			return None
		if not os.path.exists(self.path):
			return None
		column = "tile_cropped_data" if self.kind == "repo" else "tile_data"
		connection, connectionLock = self.writer().getConnection(self.path)
		with connectionLock:
			try:
				row = connection.execute("SELECT " + column + " FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?", (z, x, (1 << z) - y - 1)).fetchone()
			except sqlite3.OperationalError:
				return None
		return row[0] if row else None

	# ---------- coverage overlay ----------

	def renderCoverage(self, vz, vx, vy, dz, region):
		# one 256px overlay tile at view zoom vz showing the state of every tile at data zoom dz
		grid = self.grids.get(dz)
		d = dz - vz
		if d < 0:
			x = vx >> -d
			y = vy >> -d
			present = 1 if (grid is not None and grid.get(x, y)) else 0
			inside = region is not None and any(a <= x <= b for a, b in region.rowIntervals(dz, y))
			value = TRANSLATE_INSIDE[present] if inside else TRANSLATE_OUTSIDE[present]
			size = 1
			data = bytes([value])
			step = 1
		else:
			step = 1 << max(0, d - 8)
			size = min(256, 1 << d)
			offset = step // 2
			tx0 = (vx << d) + offset
			ty0 = (vy << d) + offset
			rows = []
			for j in range(size):
				y = ty0 + j * step
				row = grid.sampleRow(y, tx0, step, size) if grid is not None else bytearray(size)
				out = bytearray(row.translate(TRANSLATE_OUTSIDE))
				if region is not None:
					for a, b in region.rowIntervals(dz, y):
						lo = max(0, -((tx0 - a) // step))
						hi = min(size - 1, (b - tx0) // step) if b >= tx0 else -1
						if hi >= lo:
							out[lo:hi + 1] = row[lo:hi + 1].translate(TRANSLATE_INSIDE)
				rows.append(bytes(out))
			data = b"".join(rows)

		if data.count(0) == len(data):
			return None

		image = Image.frombytes("P", (size, size), data)
		image.putpalette(PALETTE, rawmode="RGBA")
		image = image.convert("RGBA").resize((256, 256), Image.NEAREST)

		pixelsPerTile = 256 // size if size > 0 and d >= 0 else 0
		if pixelsPerTile >= 12:
			draw = ImageDraw.Draw(image, "RGBA")
			for i in range(1, size):
				draw.line([(i * pixelsPerTile, 0), (i * pixelsPerTile, 255)], fill=(0, 0, 0, 60))
				draw.line([(0, i * pixelsPerTile), (255, i * pixelsPerTile)], fill=(0, 0, 0, 60))

		output = io.BytesIO()
		image.save(output, "PNG", compress_level=1)
		return output.getvalue()

	# ---------- description ----------

	def describe(self):
		manifest = self.manifest or {}
		failed = manifest.get("failed") or []
		return {
			"ds": self.token,
			"kind": self.kind,
			"path": self.path,
			"folder": self.folder,
			"pattern": self.pattern,
			"hasManifest": self.manifest is not None,
			"inferred": self.inferred,
			"hasMetadata": self.metadata is not None,
			"source": self.source,
			"outputScale": self.outputScale,
			"minZoom": self.minZoom,
			"maxZoom": self.maxZoom,
			"region": self.region.toJson() if self.region else None,
			"zoomsOnDisk": self.zoomsOnDisk(),
			"tilesOnDisk": sum(g.count for g in self.grids.values()),
			"failedCount": manifest.get("failedCount", len(failed)),
			"state": manifest.get("state"),
			"scanSeconds": self.scanSeconds,
			"ignoredFiles": self.ignoredFiles,
			"invalidFiles": self.invalidFiles,
		}


class IndexRegistry:
	# opened tile sets, looked up by token; only registered sets are served through /api/tiles

	def __init__(self, limit=4):
		self.limit = limit
		self.items = {}
		self.lock = threading.Lock()

	def add(self, index):
		key = os.path.normcase(index.path)
		with self.lock:
			for token, other in list(self.items.items()):
				if os.path.normcase(other.path) == key:
					del self.items[token]
			self.items[index.token] = index
			while len(self.items) > self.limit:
				oldest = min(self.items.values(), key=lambda i: i.lastOpened)
				del self.items[oldest.token]
		return index

	def get(self, token):
		with self.lock:
			index = self.items.get(token)
			if index is not None:
				index.lastOpened = time.time()
			return index


def openTileSet(path, pattern=None, verify=False):
	path = os.path.realpath(path)

	if os.path.isfile(path):
		ext = os.path.splitext(path)[1].lower()
		if ext == ".mbtiles":
			index = TileIndex("mbtiles", path)
		elif ext == ".repo":
			index = TileIndex("repo", path)
		else:
			raise ValueError("Not a folder, .mbtiles or .repo file")
	elif os.path.isdir(path):
		manifest = readJson(os.path.join(path, MANIFEST_NAME))
		if manifest and manifest.get("outputType") in ("mbtiles", "repo"):
			filePath = os.path.join(path, *validatePattern(manifest.get("outputFile", "")).split("/"))
			index = TileIndex(manifest["outputType"], filePath)
		else:
			if pattern is None and manifest:
				pattern = manifest.get("outputFile")
			if pattern is None:
				databases = [e.path for e in os.scandir(path) if e.is_file() and os.path.splitext(e.name)[1].lower() in (".mbtiles", ".repo")]
				hasDirs = any(e.is_dir() and e.name.isdigit() for e in os.scandir(path))
				if len(databases) == 1 and not hasDirs:
					return openTileSet(databases[0], verify=verify)
			index = TileIndex("directory", path, pattern)
	else:
		raise ValueError("Path does not exist")

	if index.kind != "directory" and not isInside(index.folder, index.path):
		raise ValueError("Tile file must be inside the chosen folder")

	index.scan(verify)
	index.loadManifest()
	return index


_listingCache = {}

def listTileSets(outputDirectory):
	result = []
	if not os.path.isdir(outputDirectory):
		return result
	for entry in os.scandir(outputDirectory):
		if not entry.is_dir():
			continue
		cached = _listingCache.get(entry.path)
		if cached and time.time() - cached[0] < 30:
			result.append(cached[1])
			continue

		tiles = 0
		modified = entry.stat().st_mtime
		databases = []
		stack = [entry.path]
		while stack:
			directory = stack.pop()
			try:
				with os.scandir(directory) as children:
					for child in children:
						if child.is_dir(follow_symlinks=False):
							stack.append(child.path)
						elif child.name.lower().endswith(TILE_EXTENSIONS):
							tiles += 1
							modified = max(modified, child.stat().st_mtime)
						elif child.name.lower().endswith((".mbtiles", ".repo")) and directory == entry.path:
							databases.append(child.name)
							modified = max(modified, child.stat().st_mtime)
			except OSError:
				continue

		manifest = readJson(os.path.join(entry.path, MANIFEST_NAME))
		for database in databases:
			if manifest is None:
				manifest = readJson(os.path.join(entry.path, database + "." + MANIFEST_NAME))
		info = {
			"name": entry.name,
			"path": entry.path,
			"tiles": tiles,
			"databases": databases,
			"modified": datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M"),
			"hasManifest": manifest is not None,
			"state": manifest.get("state") if manifest else None,
		}
		_listingCache[entry.path] = (time.time(), info)
		result.append(info)
	result.sort(key=lambda i: i["modified"], reverse=True)
	return result
