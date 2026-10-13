import os
import time
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from utils import Utils
from tile_index import FAILED_LIST_CAP, nowIso

LOG_LINES = 300
MANIFEST_SAVE_INTERVAL = 5


class DownloadJob:
	# Downloads the tiles of a region that are not on disk yet, in a server-side thread pool.
	# The tile set on disk is the source of truth, so a stopped or killed job is resumed by running again.

	def __init__(self, index, source, region, minZoom, maxZoom, outputScale, threads, mode="missing", metadataArgs=None):
		self.index = index
		self.source = source
		self.region = region
		self.minZoom = minZoom
		self.maxZoom = maxZoom
		self.outputScale = outputScale
		self.threads = max(1, min(64, threads))
		self.mode = mode
		self.metadataArgs = metadataArgs

		self.stopEvent = threading.Event()
		self.lock = threading.Lock()
		self.state = "starting"
		self.error = None
		self.startedAt = time.time()
		self.finishedAt = None
		self.pauseUntil = 0

		self.total = 0
		self.missingAtStart = 0
		self.done = 0
		self.downloaded = 0
		self.failedNow = 0
		self.skipped = 0
		self.codes = {}

		self.logSeq = 0
		self.logs = deque(maxlen=LOG_LINES)
		self.recent = deque(maxlen=5)
		self.lastManifestSave = 0

		failedList = (index.manifest or {}).get("failed") or []
		self.failed = {}
		for item in failedList:
			try:
				z, x, y = int(item[0]), int(item[1]), int(item[2])
				self.failed[(z, x, y)] = int(item[3]) if len(item) > 3 else 0
			except (ValueError, TypeError, IndexError):
				continue

		self.thread = threading.Thread(target=self.run, daemon=True)

	def start(self):
		self.thread.start()

	def stop(self):
		self.stopEvent.set()
		with self.lock:
			if self.state == "running":
				self.state = "stopping"

	def isActive(self):
		return self.thread.is_alive()

	def log(self, text):
		with self.lock:
			self.logSeq += 1
			self.logs.append((self.logSeq, text))

	def onThrottle(self, seconds):
		with self.lock:
			self.pauseUntil = max(self.pauseUntil, time.time() + seconds)

	def manifestUpdates(self, state):
		failed = sorted(self.failed.items())
		return {
			"source": self.source,
			"region": self.region.toJson(),
			"bounds": self.region.bounds,
			"minZoom": self.minZoom,
			"maxZoom": self.maxZoom,
			"outputScale": self.outputScale,
			"threads": self.threads,
			"timestamp": self.index.timestamp,
			"state": state,
			"failedCount": len(failed),
			"failed": [[z, x, y, code] for (z, x, y), code in failed[:FAILED_LIST_CAP]],
			"lastRun": {
				"started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.startedAt)),
				"mode": self.mode,
				"downloaded": self.downloaded,
				"failed": self.failedNow,
				"skippedExisting": self.skipped,
			},
		}

	def saveManifest(self, state, force=False):
		now = time.time()
		with self.lock:
			if not force and now - self.lastManifestSave < MANIFEST_SAVE_INTERVAL:
				return
			self.lastManifestSave = now
			updates = self.manifestUpdates(state)
		try:
			self.index.saveManifest(updates)
		except OSError as e:
			self.log("Could not write manifest: " + str(e))

	def tiles(self):
		if self.mode == "failed":
			for (z, x, y) in sorted(self.failed):
				yield x, y, z
			return
		for z in range(self.minZoom, self.maxZoom + 1):
			for x, y in self.region.tiles(z):
				yield x, y, z

	def run(self):
		writer = self.index.writer()
		try:
			# the disk is the source of truth: rescan right before downloading, and drop temp files of a killed run
			self.index.scan(cleanParts=True)

			if self.mode == "failed":
				self.total = len(self.failed)
				self.missingAtStart = sum(1 for (z, x, y) in self.failed if not self.index.isPresent(x, y, z))
			else:
				stats = self.index.stats(self.region, self.minZoom, self.maxZoom)
				self.total = stats["totals"]["total"]
				self.missingAtStart = stats["totals"]["missing"]
				self.index.prepareRegion(self.region, self.minZoom, self.maxZoom)

			if self.metadataArgs is not None:
				writer.addMetadata(None, *self.metadataArgs)

			with self.lock:
				self.state = "running"
			self.saveManifest("running", force=True)

			if self.missingAtStart == 0:
				self.log("Nothing to do: all " + format(self.total, ",") + " tiles are already on disk")
			else:
				self.log("Downloading " + format(self.missingAtStart, ",") + " missing of " + format(self.total, ",") + " tiles")

			slots = threading.Semaphore(self.threads * 2)
			with ThreadPoolExecutor(max_workers=self.threads, thread_name_prefix="tile") as executor:
				for x, y, z in self.tiles():
					if self.stopEvent.is_set():
						break
					if self.index.isPresent(x, y, z):
						with self.lock:
							self.skipped += 1
						continue
					while not slots.acquire(timeout=0.5):
						if self.stopEvent.is_set():
							break
					if self.stopEvent.is_set():
						break
					future = executor.submit(self.work, writer, x, y, z)
					future.add_done_callback(lambda f: slots.release())

			stopped = self.stopEvent.is_set()
			with self.lock:
				self.state = "stopped" if stopped else "done"
			if stopped:
				self.log("Stopped. " + format(self.downloaded, ",") + " tiles downloaded in this run")
			elif self.missingAtStart > 0:
				self.log("All requests are done: " + format(self.downloaded, ",") + " downloaded, " + format(self.failedNow, ",") + " failed")

		except Exception as e:
			with self.lock:
				self.state = "error"
				self.error = str(e)
			self.log("ERROR: " + str(e))
		finally:
			self.finishedAt = time.time()
			try:
				writer.close(None, self.index.folder, self.index.path, self.minZoom, self.maxZoom)
			except Exception as e:
				self.log("Could not close output: " + str(e))
			state = self.state
			if state == "done" and self.failed:
				state = "incomplete"
			self.saveManifest(state, force=True)

	def work(self, writer, x, y, z):
		if self.stopEvent.is_set():
			return

		while True:
			wait = self.pauseUntil - time.time()
			if wait <= 0:
				break
			if self.stopEvent.wait(min(wait, 1.0)):
				return

		try:
			code, data = Utils.fetchTileScaled(self.source, x, y, z, self.outputScale, self.stopEvent, self.onThrottle)
			if code == -2:
				return

			if code == 200:
				if self.index.kind == "directory":
					writer.addTileData(None, self.index.tilePath(x, y, z), data, x, y, z, self.outputScale)
				else:
					writer.addTileData(None, self.index.path, data, x, y, z, self.outputScale)
				self.index.markPresent(x, y, z)
				with self.lock:
					self.downloaded += 1
					self.done += 1
					self.failed.pop((z, x, y), None)
					self.recent.append((z, x, y))
					self.codes[200] = self.codes.get(200, 0) + 1
				self.log(str(x) + "," + str(y) + "," + str(z) + " : Tile Downloaded")
			else:
				with self.lock:
					self.failedNow += 1
					self.done += 1
					self.failed[(z, x, y)] = code
					self.codes[code] = self.codes.get(code, 0) + 1
				if code in (403, 429):
					self.log(str(x) + "," + str(y) + "," + str(z) + " : " + str(code) + " Rate limited by tile server")
				else:
					self.log(str(x) + "," + str(y) + "," + str(z) + " : " + str(code) + " Error downloading tile")
		except Exception as e:
			with self.lock:
				self.failedNow += 1
				self.done += 1
				self.failed[(z, x, y)] = -1
			self.log(str(x) + "," + str(y) + "," + str(z) + " : Error writing tile: " + str(e))

		self.saveManifest("running")

	def status(self, since=0):
		with self.lock:
			elapsed = (self.finishedAt or time.time()) - self.startedAt
			rate = self.done / elapsed if elapsed > 0 else 0
			remaining = self.missingAtStart - self.done
			return {
				"state": self.state,
				"error": self.error,
				"mode": self.mode,
				"ds": self.index.token,
				"kind": self.index.kind,
				"folder": self.index.folder,
				"path": self.index.path,
				"minZoom": self.minZoom,
				"maxZoom": self.maxZoom,
				"total": self.total,
				"missingAtStart": self.missingAtStart,
				"done": self.done,
				"downloaded": self.downloaded,
				"failed": self.failedNow,
				"failedTotal": len(self.failed),
				"skipped": self.skipped,
				"codes": {str(k): v for k, v in self.codes.items()},
				"rate": round(rate, 1),
				"eta": round(remaining / rate) if rate > 0 and remaining > 0 else None,
				"throttled": max(0, round(self.pauseUntil - time.time(), 1)),
				"elapsed": round(elapsed, 1),
				"logSeq": self.logSeq,
				"logs": [text for seq, text in self.logs if seq > since],
				"recent": list(self.recent),
				"coverageVersion": self.index.version,
			}
