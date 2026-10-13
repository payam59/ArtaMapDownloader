from mbtiles_writer import MbtilesWriter

class RepoWriter(MbtilesWriter):

	createTilesSql = "CREATE TABLE IF NOT EXISTS tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob, tile_cropped_data blob, pixel_left real, pixel_top real, pixel_right real, pixel_bottom real, has_alpha INTEGER);"

	@classmethod
	def insertTile(cls, c, x, invertedY, z, tileData, outputScale):
		c.execute("INSERT OR REPLACE INTO tiles (zoom_level, tile_column, tile_row, tile_data, tile_cropped_data, pixel_left, pixel_top, pixel_right, pixel_bottom, has_alpha) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);", [
			z, x, invertedY, None, tileData, 0, 0, 256 * outputScale, 256 * outputScale, 0
		])
