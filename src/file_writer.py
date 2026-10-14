import os
import json
import shutil

from utils import Utils

class FileWriter:

	slicer = None

	def ensureDirectory(lock, directory):
		os.makedirs(directory, exist_ok=True)
		return directory

	@staticmethod
	def addMetadata(lock, path, file, name, description, format, bounds, center, minZoom, maxZoom, profile="mercator", tileSize=256):

		FileWriter.ensureDirectory(lock, path)

		metadataPath = os.path.join(path, "metadata.json")

		# never overwrite the metadata of a folder that is being resumed
		if os.path.isfile(metadataPath):
			return

		data = [
			("name", name),
			("description", description),
			("format", format),
			("bounds", ','.join(map(str, bounds))),
			("center", ','.join(map(str, center))),
			("minzoom", minZoom),
			("maxzoom", maxZoom),
			("profile", profile),
			("tilesize", str(tileSize)),
			("scheme", "xyz"),
			("generator", "Arta Map Downloader (Arta Cyber Inc.)"),
			("type", "overlay"),
			("attribution", "Arta Map Downloader (Arta Cyber Inc.)"),
		]

		Utils.writeFileAtomic(metadataPath, json.dumps(dict(data)).encode("utf-8"))

		return

	@staticmethod
	def addTileData(lock, filePath, tileData, x, y, z, outputScale):

		fileDirectory = os.path.dirname(filePath)
		FileWriter.ensureDirectory(lock, fileDirectory)

		Utils.writeFileAtomic(filePath, tileData)

	@staticmethod
	def addTile(lock, filePath, sourcePath, x, y, z, outputScale):

		fileDirectory = os.path.dirname(filePath)
		FileWriter.ensureDirectory(lock, fileDirectory)

		tempPath = filePath + ".part-" + Utils.randomString()
		shutil.copyfile(sourcePath, tempPath)
		os.replace(tempPath, filePath)

		return

	@staticmethod
	def exists(filePath, x, y, z):
		# a 0-byte file is a failed write, not a tile
		try:
			return os.path.getsize(filePath) > 0
		except OSError:
			return False


	@staticmethod
	def close(lock, path, file, minZoom, maxZoom):
		#TODO recalculate bounds and center
		return
