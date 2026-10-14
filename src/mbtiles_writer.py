import sqlite3
import os
import threading
from utils import Utils

# One shared connection per database file, guarded by its own lock.
# Opening a connection per tile was slow and leaked handles.
_connections = {}
_connectionsLock = threading.Lock()

class MbtilesWriter:

	createTilesSql = "CREATE TABLE IF NOT EXISTS tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob);"

	def ensureDirectory(lock, directory):
		os.makedirs(directory, exist_ok=True)
		return directory

	@staticmethod
	def getConnection(file):
		key = os.path.normcase(os.path.abspath(file))
		with _connectionsLock:
			entry = _connections.get(key)
			if entry is None:
				connection = sqlite3.connect(file, check_same_thread=False, timeout=30)
				try:
					connection.execute("PRAGMA journal_mode=WAL;")
					connection.execute("PRAGMA synchronous=NORMAL;")
				except sqlite3.DatabaseError:
					pass
				entry = (connection, threading.Lock())
				_connections[key] = entry
			return entry

	@staticmethod
	def closeConnection(file):
		key = os.path.normcase(os.path.abspath(file))
		with _connectionsLock:
			entry = _connections.pop(key, None)
		if entry is not None:
			connection, connectionLock = entry
			with connectionLock:
				try:
					connection.commit()
					connection.execute("PRAGMA wal_checkpoint(TRUNCATE);")
				except sqlite3.DatabaseError:
					pass
				connection.close()

	@classmethod
	def createSchema(cls, connection):
		c = connection.cursor()
		c.execute("CREATE TABLE IF NOT EXISTS metadata (name text, value text);")
		c.execute(cls.createTilesSql)
		for sql in ("CREATE UNIQUE INDEX IF NOT EXISTS tile_index on tiles (zoom_level, tile_column, tile_row);", "CREATE UNIQUE INDEX IF NOT EXISTS metadata_name ON metadata (name);"):
			try:
				c.execute(sql)
			except sqlite3.DatabaseError:
				pass
		connection.commit()

	@classmethod
	def addMetadata(cls, lock, path, file, name, description, format, bounds, center, minZoom, maxZoom, profile="mercator", tileSize=256):

		cls.ensureDirectory(lock, path)

		connection, connectionLock = cls.getConnection(file)
		with connectionLock:
			cls.createSchema(connection)

			# INSERT OR IGNORE keeps the metadata of an existing file intact when resuming
			connection.executemany("INSERT OR IGNORE INTO metadata (name, value) VALUES (?, ?);", [
				("name", name),
				("description", description),
				("format", format),
				("bounds", ','.join(map(str, bounds))),
				("center", ','.join(map(str, center))),
				("minzoom", minZoom),
				("maxzoom", maxZoom),
				("profile", profile),
				("tilesize", str(tileSize)),
				("scheme", "tms"),
				("generator", "Arta Map Downloader (Arta Cyber Inc.)"),
				("type", "overlay"),
				("attribution", "Arta Map Downloader (Arta Cyber Inc.)"),
			])
			connection.commit()

	@classmethod
	def insertTile(cls, c, x, invertedY, z, tileData, outputScale):
		c.execute("INSERT OR REPLACE INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?);", [
			z, x, invertedY, tileData
		])

	@classmethod
	def addTileData(cls, lock, filePath, tileData, x, y, z, outputScale):

		invertedY = (2 ** z) - y - 1

		connection, connectionLock = cls.getConnection(filePath)
		with connectionLock:
			c = connection.cursor()
			cls.insertTile(c, x, invertedY, z, tileData, outputScale)
			connection.commit()

	@classmethod
	def addTile(cls, lock, filePath, sourcePath, x, y, z, outputScale):

		fileDirectory = os.path.dirname(filePath)
		cls.ensureDirectory(lock, fileDirectory)

		with open(sourcePath, "rb") as readFile:
			tileData = readFile.read()

		cls.addTileData(lock, filePath, tileData, x, y, z, outputScale)

	@classmethod
	def exists(cls, filePath, x, y, z):
		invertedY = (2 ** z) - y - 1

		if not os.path.exists(filePath):
			return False

		connection, connectionLock = cls.getConnection(filePath)
		with connectionLock:
			try:
				c = connection.execute("SELECT 1 FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ? LIMIT 1", (z, x, invertedY))
				return c.fetchone() is not None
			except sqlite3.OperationalError:
				return False

	@classmethod
	def close(cls, lock, path, file, minZoom, maxZoom):

		if not os.path.exists(file):
			return

		connection, connectionLock = cls.getConnection(file)
		with connectionLock:
			c = connection.cursor()
			c.execute("SELECT min(tile_row), max(tile_row), min(tile_column), max(tile_column) from tiles WHERE zoom_level = ?", [maxZoom])

			row = c.fetchone()
			if row is not None and row[0] is not None:
				minY, maxY, minX, maxX = row
				minY = (2 ** maxZoom) - minY - 1
				maxY = (2 ** maxZoom) - maxY - 1

				minLat, minLon = Utils.num2deg(minX, minY, maxZoom)
				maxLat, maxLon = Utils.num2deg(maxX+1, maxY+1, maxZoom)

				bounds = [minLon, minLat, maxLon, maxLat]
				boundsString = ','.join(map(str, bounds))

				center = [(minLon + maxLon)/2, (minLat + maxLat)/2, maxZoom]
				centerString = ','.join(map(str, center))

				c.execute("UPDATE metadata SET value = ? WHERE name = 'bounds'", [boundsString])
				c.execute("UPDATE metadata SET value = ? WHERE name = 'center'", [centerString])

			connection.commit()

		cls.closeConnection(file)

