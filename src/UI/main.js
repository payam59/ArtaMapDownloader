var mapView;

$(function() {

	var map = null;
	var draw = null;
	var geocoder = null;
	var bar = null;

	// the download itself runs on the server (/api/job/*); the page only polls its status
	var pollTimer = null;
	var logSeq = 0;
	var logLines = [];
	var MAX_LOG_LINES = 300;
	var lastRecent = "";
	var lastCoverageVersion = -1;
	var lastCoverageRefresh = 0;
	var finishedState = null;

	// the tile set opened in the resume panel
	var dataset = null;
	var statsTimer = null;
	var mapReady = false;

	var sources = {

		"Bing Maps": "http://ecn.t0.tiles.virtualearth.net/tiles/r{quad}.jpeg?g=129&mkt=en&stl=H",
		"Bing Maps Satellite": "http://ecn.t0.tiles.virtualearth.net/tiles/a{quad}.jpeg?g=129&mkt=en&stl=H",
		"Bing Maps Hybrid": "http://ecn.t0.tiles.virtualearth.net/tiles/h{quad}.jpeg?g=129&mkt=en&stl=H",

		"div-1B": "",

		"Google Maps": "https://mt0.google.com/vt?lyrs=m&x={x}&s=&y={y}&z={z}",
		"Google Maps Satellite": "https://mt0.google.com/vt?lyrs=s&x={x}&s=&y={y}&z={z}",
		"Google Maps Hybrid": "https://mt0.google.com/vt?lyrs=h&x={x}&s=&y={y}&z={z}",
		"Google Maps Terrain": "https://mt0.google.com/vt?lyrs=p&x={x}&s=&y={y}&z={z}",

		"div-2": "",

		"Open Street Maps": "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png",
		"Open Cycle Maps": "http://a.tile.opencyclemap.org/cycle/{z}/{x}/{y}.png",

		"div-3": "",

		"ESRI World Imagery": "https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
		"Carto Light": "https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",

	};

	function api(type, url, data) {
		var options = {
			url: url,
			async: true,
			timeout: 60 * 1000,
			type: type,
			dataType: 'json',
		};
		if(data !== undefined) {
			options.contentType = "application/json";
			options.data = JSON.stringify(data);
		}
		return $.ajax(options);
	}

	function apiError(xhr, fallback) {
		if(xhr && xhr.responseJSON && xhr.responseJSON.message) {
			return xhr.responseJSON.message;
		}
		return fallback;
	}

	function initializeMap() {

		// A Mapbox token is only required if you want the search/geocoder feature to work.
		// Get a free token at https://account.mapbox.com/ and replace the empty string below.
		mapboxgl.accessToken = '';

		map = new mapboxgl.Map({
			container: 'map-view',
			style: {
				version: 8,
				sources: {
					'osm': {
						type: 'raster',
						tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
						tileSize: 256,
						attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
					}
				},
				layers: [{
					id: 'osm-tiles',
					type: 'raster',
					source: 'osm'
				}]
			},
			center: [-73.983652, 40.755024],
			zoom: 12
		});

		// isStyleLoaded() stays false while base tiles load, so track the style load once
		map.on('load', function() {
			mapReady = true;
			renderRegion();
			refreshCoverage();
		});

		if (mapboxgl.accessToken) {
			geocoder = new MapboxGeocoder({ accessToken: mapboxgl.accessToken });
			map.addControl(geocoder);
		}
	}

	function initializeMaterialize() {
		$('select').formSelect();
		$('.dropdown-trigger').dropdown({
			constrainWidth: false,
		});

		// About / Contact: plain Materialize modals (Esc, backdrop click and Close all dismiss them)
		M.Modal.init(document.querySelectorAll('.info-modal'), {});

		api("get", "/api/version").done(function(result) {
			$(".app-version").text(result.version);
		});
	}

	function initializeSources() {

		$(".source-list").each(function() {

			var dropdown = $(this);
			var target = $(dropdown.attr("data-input"));

			for(var key in sources) {
				var url = sources[key];

				if(url == "") {
					dropdown.append("<hr/>");
					continue;
				}

				var item = $("<li><a></a></li>");
				item.attr("data-url", url);
				item.find("a").text(key);

				item.click(function() {
					var url = $(this).attr("data-url");
					target.val(url);
					M.updateTextFields();
				})

				dropdown.append(item);
			}
		});
	}

	function initializeSearch() {
		$("#search-form").submit(function(e) {
			var location = $("#location-box").val();
			if (geocoder) geocoder.query(location);

			e.preventDefault();
		})
	}

	function initializeMoreOptions() {

		$("#more-options-toggle").click(function() {
			$("#more-options").toggle();
		})

		var outputFileBox = $("#output-file-box")

		function updateStitchCheckboxState() {
			var outputType = $("#output-type").val();
			if (outputType !== "directory") {
				$("#stitch-checkbox").prop("checked", false).prop("disabled", true);
			} else {
				$("#stitch-checkbox").prop("disabled", false);
			}
		}

		$("#output-type").change(function() {
			var outputType = $("#output-type").val();
			if(outputType == "mbtiles") {
				outputFileBox.val("tiles.mbtiles")
			} else if(outputType == "repo") {
				outputFileBox.val("tiles.repo")
			} else if(outputType == "directory") {
				outputFileBox.val("{z}/{x}/{y}.png")
			}
			updateStitchCheckboxState();
		})

		updateStitchCheckboxState();

	}

	function pollStitchStatus() {
		return new Promise(function(resolve) {
			var lastMessage = "";
			var interval = setInterval(function() {
				$.ajax({
					url: "/stitch-status",
					async: true,
					type: "get",
					dataType: 'json',
				}).done(function(status) {
					if (status.message !== lastMessage) {
						lastMessage = status.message;
						logItemRaw(status.message);
					}
					if (status.state === "done") {
						clearInterval(interval);
						for (var i = 0; i < status.files.length; i++) {
							logItemRaw("Saved: " + status.files[i]);
						}
						resolve();
					} else if (status.state === "error") {
						clearInterval(interval);
						resolve();
					}
				}).fail(function() {
					clearInterval(interval);
					logItemRaw("Could not reach stitch status endpoint.");
					resolve();
				});
			}, 2000);
		});
	}

	function initializeRectangleTool() {

		var modes = MapboxDraw.modes;
		modes.draw_rectangle = DrawRectangle.default;

		draw = new MapboxDraw({
			modes: modes
		});
		map.addControl(draw);

		// the draw plugin is only used to draw the first rectangle; after that the page owns it
		// (mapbox-gl-draw's vertex editing would let a corner drag skew it into a non-rectangle)
		map.on('draw.create', function (e) {
			M.Toast.dismissAll();
			var coordinates = e.features[0].geometry.coordinates[0];
			var lngs = coordinates.map(function(c) { return c[0]; });
			var lats = coordinates.map(function(c) { return c[1]; });
			draw.deleteAll();
			draw.changeMode('simple_select');
			setRegion([Math.min.apply(null, lngs), Math.min.apply(null, lats), Math.max.apply(null, lngs), Math.max.apply(null, lats)], true);
		});

		map.on('mousemove', onEditMove);
		map.on('mousedown', onEditDown);
		map.on('mouseup', onEditUp);
		$(document).on('mouseup', onEditUp);

		$("#rectangle-draw-button").click(function() {
			startDrawing();
		})

		$("#zoom-from-box, #zoom-to-box").on("change keyup", updateEstimate);

	}

	function startDrawing() {
		removeGrid();
		setRegion(null, false);
		draw.deleteAll();
		draw.changeMode('draw_rectangle');

		M.Toast.dismissAll();
		M.toast({html: 'Click two points on the map to make a rectangle.', displayLength: 7000})
	}

	// ---------- editable region rectangle ----------

	var region = null;		// [west, south, east, north]
	var editDrag = null;
	var dragPanDisabled = false;
	var regionTimer = null;
	var MAX_LAT = 85.0511287798;

	function setRegion(bounds, notify) {
		region = bounds ? normalizeRegion(bounds) : null;
		renderRegion();
		updateEstimate();
		if(notify) {
			regionChanged();
		}
	}

	function normalizeRegion(r) {
		var w = Math.max(-180, Math.min(180, Math.min(r[0], r[2])));
		var e = Math.max(-180, Math.min(180, Math.max(r[0], r[2])));
		var s = Math.max(-MAX_LAT, Math.min(MAX_LAT, Math.min(r[1], r[3])));
		var n = Math.max(-MAX_LAT, Math.min(MAX_LAT, Math.max(r[1], r[3])));
		return [w, s, e, n];
	}

	function renderRegion() {
		if(!mapReady) {
			return;
		}

		var features = [];
		if(region) {
			var w = region[0], s = region[1], e = region[2], n = region[3];
			features.push({type: 'Feature', properties: {role: 'box'}, geometry: {type: 'Polygon', coordinates: [[[w, n], [e, n], [e, s], [w, s], [w, n]]]}});
			[[w, n], [e, n], [e, s], [w, s]].forEach(function(c) {
				features.push({type: 'Feature', properties: {role: 'corner'}, geometry: {type: 'Point', coordinates: c}});
			});
			[[(w + e) / 2, n], [e, (s + n) / 2], [(w + e) / 2, s], [w, (s + n) / 2]].forEach(function(c) {
				features.push({type: 'Feature', properties: {role: 'edge'}, geometry: {type: 'Point', coordinates: c}});
			});
			features.push({type: 'Feature', properties: {role: 'move'}, geometry: {type: 'Point', coordinates: [(w + e) / 2, (s + n) / 2]}});
		}
		var data = {type: 'FeatureCollection', features: features};

		var source = map.getSource('region-edit');
		if(source) {
			source.setData(data);
			return;
		}

		map.addSource('region-edit', {type: 'geojson', data: data});
		map.addLayer({id: 'region-fill', type: 'fill', source: 'region-edit', filter: ['==', 'role', 'box'], paint: {'fill-color': '#ff9f1a', 'fill-opacity': 0.06}});
		map.addLayer({id: 'region-line', type: 'line', source: 'region-edit', filter: ['==', 'role', 'box'], paint: {'line-color': '#fa8231', 'line-width': 2}});
		map.addLayer({id: 'region-handles', type: 'circle', source: 'region-edit', filter: ['!=', 'role', 'box'], paint: {
			'circle-radius': ['match', ['get', 'role'], 'move', 8, 'corner', 6, 5],
			'circle-color': ['match', ['get', 'role'], 'move', '#fa8231', '#ffffff'],
			'circle-stroke-color': '#fa8231',
			'circle-stroke-width': 2,
		}});
	}

	// hit test in screen pixels, so it does not depend on what the map has rendered yet
	function hitTest(point) {
		if(!region || draw.getMode() == 'draw_rectangle') {
			return null;
		}
		var w = region[0], s = region[1], e = region[2], n = region[3];
		var nw = map.project([w, n]), ne = map.project([e, n]), se = map.project([e, s]), sw = map.project([w, s]);
		var center = map.project([(w + e) / 2, (s + n) / 2]);

		function near(p, q, d) {
			return Math.abs(p.x - q.x) <= d && Math.abs(p.y - q.y) <= d;
		}

		if(near(point, center, 10)) return 'move';
		if(near(point, nw, 9)) return 'nw';
		if(near(point, ne, 9)) return 'ne';
		if(near(point, se, 9)) return 'se';
		if(near(point, sw, 9)) return 'sw';
		var inX = point.x > nw.x && point.x < ne.x;
		var inY = point.y > nw.y && point.y < sw.y;
		if(inX && Math.abs(point.y - nw.y) <= 6) return 'n';
		if(inX && Math.abs(point.y - sw.y) <= 6) return 's';
		if(inY && Math.abs(point.x - nw.x) <= 6) return 'w';
		if(inY && Math.abs(point.x - ne.x) <= 6) return 'e';
		return null;
	}

	var CURSORS = {move: 'move', nw: 'nwse-resize', se: 'nwse-resize', ne: 'nesw-resize', sw: 'nesw-resize', n: 'ns-resize', s: 'ns-resize', e: 'ew-resize', w: 'ew-resize'};

	function setDragPan(enabled) {
		if(enabled && dragPanDisabled) {
			map.dragPan.enable();
			dragPanDisabled = false;
		} else if(!enabled && !dragPanDisabled) {
			map.dragPan.disable();
			dragPanDisabled = true;
		}
	}

	function onEditMove(e) {
		if(editDrag) {
			var r = editDrag.startRegion.slice();
			var lng = e.lngLat.lng;
			var lat = Math.max(-MAX_LAT, Math.min(MAX_LAT, e.lngLat.lat));
			if(editDrag.mode == 'move') {
				var dLng = lng - editDrag.start.lng;
				var dLat = lat - editDrag.start.lat;
				dLat = Math.max(-MAX_LAT - r[1], Math.min(MAX_LAT - r[3], dLat));
				r = [r[0] + dLng, r[1] + dLat, r[2] + dLng, r[3] + dLat];
			} else {
				if(editDrag.mode.indexOf('n') >= 0) r[3] = lat;
				if(editDrag.mode.indexOf('s') >= 0) r[1] = lat;
				if(editDrag.mode.indexOf('w') >= 0) r[0] = lng;
				if(editDrag.mode.indexOf('e') >= 0) r[2] = lng;
			}
			region = normalizeRegion(r);
			renderRegion();
			updateEstimate();
			// light update while dragging: the table follows, one request in flight at a time
			if(dataset && $("#resume-sidebar").is(":visible")) {
				requestStats();
			}
			return;
		}

		var hit = hitTest(e.point);
		map.getCanvas().style.cursor = hit ? CURSORS[hit] : '';
		setDragPan(!hit);
	}

	function onEditDown(e) {
		var hit = hitTest(e.point);
		if(!hit) {
			return;
		}
		e.preventDefault();
		setDragPan(false);
		editDrag = {mode: hit, start: e.lngLat, startRegion: region.slice()};
	}

	function onEditUp() {
		if(!editDrag) {
			return;
		}
		editDrag = null;
		setDragPan(true);
		regionChanged();
	}

	function updateEstimate() {
		var box = $("#region-estimate");
		var minZoom = getMinZoom();
		var maxZoom = getMaxZoom();
		if(!region) {
			box.text("");
			return;
		}
		if(isNaN(minZoom) || isNaN(maxZoom)) {
			box.text("Enter zoom levels");
			return;
		}
		box.text(countTiles(minZoom, maxZoom).toLocaleString() + " tiles at zoom " + minZoom + "-" + maxZoom + ". Drag the corners, edges or the centre dot to change it.");
	}

	function initializeGridPreview() {
		$("#grid-preview-button").click(previewGrid);

		map.on('click', showTilePopup);
	}

	function showTilePopup(e) {

		if(!e.originalEvent.ctrlKey) {
			return;
		}

		var maxZoom = getMaxZoom();

		var x = long2tile(e.lngLat.lng, maxZoom);
		var y = lat2tile(e.lngLat.lat, maxZoom);

		var content = "X, Y, Z<br/><b>" + x + ", " + y + ", " + maxZoom + "</b><hr/>";
		content += "Lat, Lng<br/><b>" + e.lngLat.lat + ", " + e.lngLat.lng + "</b>";

        new mapboxgl.Popup()
            .setLngLat(e.lngLat)
            .setHTML(content)
            .addTo(map);

	}

	function long2tile(lon,zoom) {
		var n = Math.pow(2, zoom);
		return Math.min(n - 1, Math.max(0, Math.floor((lon+180)/360*n)));
	}

	function lat2tile(lat,zoom)  {
		var n = Math.pow(2, zoom);
		lat = Math.max(-85.0511287798, Math.min(85.0511287798, lat));
		return Math.min(n - 1, Math.max(0, Math.floor((1-Math.log(Math.tan(lat*Math.PI/180) + 1/Math.cos(lat*Math.PI/180))/Math.PI)/2 *n)));
	}

	function tile2long(x,z) {
		return (x/Math.pow(2,z)*360-180);
	}

	function tile2lat(y,z) {
		var n=Math.PI-2*Math.PI*y/Math.pow(2,z);
		return (180/Math.PI*Math.atan(0.5*(Math.exp(n)-Math.exp(-n))));
	}

	function getMinZoom() {
		return Math.min(parseInt($("#zoom-from-box").val()), parseInt($("#zoom-to-box").val()));
	}

	function getMaxZoom() {
		return Math.max(parseInt($("#zoom-from-box").val()), parseInt($("#zoom-to-box").val()));
	}

	function hasRegion() {
		return region != null;
	}

	function getBounds() {
		return new mapboxgl.LngLatBounds([region[0], region[1]], [region[2], region[3]]);
	}

	function getBoundsArray() {
		return region.slice();
	}

	function getPolygon() {
		var w = region[0], s = region[1], e = region[2], n = region[3];
		return [[w, n], [e, n], [e, s], [w, s], [w, n]];
	}

	// tile range of the drawn rectangle; the rectangle tool always draws axis-aligned boxes,
	// so every tile in the range touches the selection and no per-tile polygon test is needed
	function getTileRange(zoom) {
		var bounds = getBounds();
		return {
			x0: long2tile(bounds.getSouthWest().lng, zoom),
			y0: lat2tile(bounds.getNorthEast().lat, zoom),
			x1: long2tile(bounds.getNorthEast().lng, zoom),
			y1: lat2tile(bounds.getSouthWest().lat, zoom),
		};
	}

	function countTiles(minZoom, maxZoom) {
		var total = 0;
		for(var z = minZoom; z <= maxZoom; z++) {
			var r = getTileRange(z);
			total += (r.x1 - r.x0 + 1) * (r.y1 - r.y0 + 1);
		}
		return total;
	}

	function removeGrid() {
		removeLayer("grid-preview");
	}

	function previewGrid() {

		if(!hasRegion()) {
			M.toast({html: 'You need to select a region first.', displayLength: 3000})
			return;
		}

		var maxZoom = getMaxZoom();
		var r = getTileRange(maxZoom);

		// a grid is (columns + rows) lines, not one polygon per tile
		var lines = [];
		var north = tile2lat(r.y0, maxZoom);
		var south = tile2lat(r.y1 + 1, maxZoom);
		var west = tile2long(r.x0, maxZoom);
		var east = tile2long(r.x1 + 1, maxZoom);
		var step = Math.max(1, Math.ceil(Math.max(r.x1 - r.x0 + 1, r.y1 - r.y0 + 1) / 400));

		for(var x = r.x0; x <= r.x1 + 1; x += step) {
			lines.push([[tile2long(x, maxZoom), north], [tile2long(x, maxZoom), south]]);
		}
		for(var y = r.y0; y <= r.y1 + 1; y += step) {
			lines.push([[west, tile2lat(y, maxZoom)], [east, tile2lat(y, maxZoom)]]);
		}
		lines.push([[west, north], [east, north], [east, south], [west, south], [west, north]]);

		removeGrid();

		var totalTiles = countTiles(getMinZoom(), getMaxZoom());
		M.toast({html: 'Total ' + totalTiles.toLocaleString() + ' tiles in the region.', displayLength: 5000})

		if(!mapReady) {
			return;
		}

		map.addLayer({
			'id': "grid-preview",
			'type': 'line',
			'source': {
				'type': 'geojson',
				'data': {type: 'Feature', properties: {}, geometry: {type: 'MultiLineString', coordinates: lines}},
			},
			'layout': {},
			'paint': {
				"line-color": "#fa8231",
				"line-width": step > 1 ? 1 : 3,
			}
		});

	}

	function removeLayer(id) {
		if(map.getLayer(id) != null) {
			map.removeLayer(id);
		}
		if(map.getSource(id) != null) {
			map.removeSource(id);
		}
	}

	function initializeDownloader() {

		bar = new ProgressBar.Circle($('#progress-radial').get(0), {
			strokeWidth: 12,
			easing: 'easeOut',
			duration: 200,
			trailColor: '#eee',
			trailWidth: 1,
			from: {color: '#0fb9b1', a:0},
			to: {color: '#20bf6b', a:1},
			svgStyle: null,
			step: function(state, circle) {
				circle.path.setAttribute('stroke', state.color);
			}
		});

		$("#download-button").click(startDownloading)
		$("#stop-button").click(stopDownloading)
	}

	function showDownloadSidebar() {
		$("#main-sidebar").hide();
		$("#resume-sidebar").hide();
		$("#download-sidebar").show();
		$(".tile-strip").html("");
		$("#stop-button").html("STOP").prop("disabled", false);
		$("#download-phase-title").text("Downloading tiles");
		$("#download-phase-hint").text("Please wait...");
		lastRecent = "";
		finishedState = null;
		removeGrid();
		clearLogs();
		M.Toast.dismissAll();
		updateProgress(0, 0);
	}

	async function startDownloading() {

		if(!hasRegion()) {
			M.toast({html: 'You need to select a region first.', displayLength: 3000})
			return;
		}

		var request = {
			mode: "new",
			minZoom: getMinZoom(),
			maxZoom: getMaxZoom(),
			outputDirectory: $("#output-directory-box").val(),
			outputFile: $("#output-file-box").val(),
			outputType: $("#output-type").val(),
			outputScale: parseInt($("#output-scale").val()),
			threads: parseInt($("#parallel-threads-box").val()),
			source: $("#source-box").val(),
			timestamp: Date.now(),
			bounds: getBoundsArray(),
			polygon: getPolygon(),
		};

		startJob(request);
	}

	function startJob(request) {
		api("post", "/api/job/start", request).done(function(result) {
			showDownloadSidebar();
			if(dataset == null || dataset.ds != result.ds) {
				dataset = result;
			}
			setCoverageDataset(result.ds);
			startPolling();
		}).fail(function(xhr) {
			M.toast({html: apiError(xhr, 'Could not start the download.'), displayLength: 6000});
		});
	}

	function startPolling() {
		logSeq = 0;
		if(pollTimer) {
			clearTimeout(pollTimer);
		}
		pollStatus();
	}

	function pollStatus() {
		api("get", "/api/job/status?since=" + logSeq).done(function(status) {
			renderStatus(status);
			if(status.state == "running" || status.state == "starting" || status.state == "stopping") {
				pollTimer = setTimeout(pollStatus, 1000);
			} else {
				pollTimer = null;
				jobFinished(status);
			}
		}).fail(function() {
			logItemRaw("Lost contact with the server, retrying...");
			pollTimer = setTimeout(pollStatus, 3000);
		});
	}

	function renderStatus(status) {

		if(status.logs && status.logs.length) {
			for(var i = 0; i < status.logs.length; i++) {
				logLines.push(status.logs[i]);
			}
			logSeq = status.logSeq;
			if(logLines.length > MAX_LOG_LINES) {
				logLines = logLines.slice(logLines.length - MAX_LOG_LINES);
			}
			var logger = $('#log-view');
			logger.val(logLines.join('\n'));
			logger.scrollTop(logger[0].scrollHeight);
		}

		updateProgress(status.done || 0, status.missingAtStart || 0);

		var hint = "";
		if(status.total) {
			hint = (status.total - status.missingAtStart).toLocaleString() + " of " + status.total.toLocaleString() + " tiles were already on disk";
		}
		if(status.rate) {
			hint += "<br/>" + status.rate + " tiles/s";
			if(status.eta) {
				hint += ", about " + formatDuration(status.eta) + " left";
			}
		}
		if(status.failed) {
			hint += "<br/>" + status.failed.toLocaleString() + " failed in this run";
		}
		if(status.throttled > 0) {
			hint += "<br/>Rate limited, pausing " + status.throttled + "s";
		}
		if(hint) {
			$("#download-phase-hint").html(hint);
		}

		var recentKey = JSON.stringify(status.recent || []);
		if(recentKey != lastRecent) {
			lastRecent = recentKey;
			var strip = $(".tile-strip");
			strip.html("");
			var recent = (status.recent || []).slice().reverse();
			for(var j = 0; j < recent.length; j++) {
				var t = recent[j];
				strip.append($("<img/>").attr('src', "/api/tiles/" + t[0] + "/" + t[1] + "/" + t[2] + "?ds=" + status.ds));
			}
		}

		var now = Date.now();
		if(status.coverageVersion != lastCoverageVersion && now - lastCoverageRefresh > 4000) {
			lastCoverageVersion = status.coverageVersion;
			lastCoverageRefresh = now;
			refreshCoverage();
		}
	}

	function formatDuration(seconds) {
		if(seconds < 90) return Math.round(seconds) + "s";
		if(seconds < 5400) return Math.round(seconds / 60) + " min";
		return (seconds / 3600).toFixed(1) + " h";
	}

	async function jobFinished(status) {

		finishedState = status.state;
		refreshCoverage();

		if(status.state == "stopped") {
			$("#download-phase-title").text("Stopped");
			$("#stop-button").html("BACK").prop("disabled", false);
			return;
		}

		if(status.state == "error") {
			$("#download-phase-title").text("Error");
			logItemRaw("Error: " + status.error);
			$("#stop-button").html("CLOSE").prop("disabled", false);
			return;
		}

		updateProgress(status.missingAtStart, status.missingAtStart);
		$("#download-phase-title").text(status.failedTotal ? "Finished with failures" : "Finished");

		if ($("#stitch-checkbox").is(":checked") && status.kind == "directory" && !$("#resume-sidebar").data("from-resume")) {
			$("#stop-button").html("STITCHING...").prop("disabled", true);
			$("#download-phase-title").text("Stitching tiles");
			$("#download-phase-hint").text("Building image from tiles, please wait...");

			var stitchData = new FormData();
			stitchData.append('folder', status.folder);
			stitchData.append('minZoom', status.minZoom);
			stitchData.append('maxZoom', status.maxZoom);

			await $.ajax({
				url: "/stitch-tiles",
				async: true,
				timeout: 30 * 1000,
				type: "post",
				contentType: false,
				processData: false,
				data: stitchData,
				dataType: 'json',
			});

			await pollStitchStatus();

			$("#download-phase-title").text("Finished");
			$("#stop-button").html("FINISH").prop("disabled", false);
		} else {
			$("#stop-button").html("FINISH").prop("disabled", false);
		}
	}

	function updateProgress(value, total) {
		var progress = total > 0 ? value / total : (finishedState ? 1 : 0);

		// set, not animate: progressbar.js leaves an unhandled promise rejection every time a running animation is replaced
		bar.set(Math.min(1, progress));
		bar.setText(Math.round(progress * 100) + '<span>%</span>');

		$("#progress-subtitle").html(value.toLocaleString() + " <span>out of</span> " + total.toLocaleString())
	}

	function logItemRaw(text) {
		logLines.push(text);
		if(logLines.length > MAX_LOG_LINES) {
			logLines.shift();
		}
		var logger = $('#log-view');
		logger.val(logLines.join('\n'));
		logger.scrollTop(logger[0].scrollHeight);
	}

	function clearLogs() {
		logLines = [];
		$('#log-view').val('');
	}

	function stopDownloading() {

		if(finishedState == null) {
			$("#stop-button").html("STOPPING...").prop("disabled", true);
			api("post", "/api/job/stop", {});
			return;
		}

		// finished, stopped or failed: go back, and offer to resume a folder that is not complete
		$("#download-sidebar").hide();
		var openPath = dataset ? dataset.path : null;
		if(finishedState == "stopped" || finishedState == "error" || $("#resume-sidebar").data("from-resume")) {
			showResumeSidebar();
			if(openPath) {
				openDataset({path: openPath});
			}
		} else {
			$("#main-sidebar").show();
			clearCoverage();
		}
		finishedState = null;
	}

	// ---------- resume / open existing folder ----------

	function initializeResume() {

		$("#open-existing-button").click(function() {
			showResumeSidebar();
		});

		$("#resume-close-button").click(function() {
			$("#resume-sidebar").hide().data("from-resume", false);
			$("#main-sidebar").show();
			clearCoverage();
			dataset = null;
		});

		$("#open-path-form").submit(function(e) {
			e.preventDefault();
			var path = $("#open-path-box").val().trim();
			if(path == "") {
				return;
			}
			openDataset({path: path});
		});

		$("#resume-zoom-from-box, #resume-zoom-to-box").on("change keyup", scheduleStats);
		// the overlay shows the deepest zoom being downloaded unless picked by hand
		$("#resume-zoom-to-box").on("change", function() {
			var zooms = getResumeZooms();
			if(zooms && $("#coverage-zoom option[value='" + zooms[1] + "']").length) {
				$("#coverage-zoom").val(zooms[1]);
				refreshCoverage();
			}
		});
		$("#show-coverage-checkbox, #show-imagery-checkbox").change(function() {
			refreshCoverage();
		});
		$("#coverage-zoom").change(refreshCoverage);

		$("#resume-button").click(function() {
			resumeDataset("missing");
		});
		$("#retry-failed-button").click(function() {
			resumeDataset("failed");
		});
		$("#fit-region-button").click(fitRegion);
	}

	function showResumeSidebar() {
		$("#main-sidebar").hide();
		$("#download-sidebar").hide();
		$("#resume-sidebar").show().data("from-resume", true);
		loadDatasetList();
	}

	function loadDatasetList() {
		var list = $("#dataset-list");
		list.html("<div class='hints'>Loading...</div>");
		api("get", "/api/datasets").done(function(result) {
			list.html("");
			$("#output-root").text(result.outputDirectory);
			if(result.items.length == 0) {
				list.html("<div class='hints'>No folders in the output directory yet.</div>");
			}
			for(var i = 0; i < result.items.length; i++) {
				var item = result.items[i];
				var row = $("<a class='collection-item dataset-item' href='javascript:void(0)'></a>");
				var title = $("<b></b>").text(item.name);
				var info = $("<div class='dataset-info-line'></div>").text(
					item.tiles.toLocaleString() + " tiles" +
					(item.databases.length ? ", " + item.databases.join(", ") : "") +
					" | " + item.modified +
					(item.hasManifest ? " | " + (item.state || "manifest") : " | no manifest"));
				row.append(title).append(info);
				row.data("name", item.name);
				row.click(function() {
					openDataset({name: $(this).data("name")});
				});
				list.append(row);
			}
		}).fail(function(xhr) {
			list.html("");
			M.toast({html: apiError(xhr, 'Could not list the output folder.'), displayLength: 5000});
		});
	}

	function openDataset(request) {
		request.verify = $("#verify-checkbox").is(":checked");
		$("#dataset-panel").hide();
		$("#dataset-loading").show();
		api("post", "/api/open", request).done(function(result) {
			$("#dataset-loading").hide();
			showDataset(result);
		}).fail(function(xhr) {
			$("#dataset-loading").hide();
			M.toast({html: apiError(xhr, 'Could not open that folder.'), displayLength: 6000});
		});
	}

	function showDataset(result) {
		dataset = result;

		$("#dataset-panel").show();
		$("#dataset-path").text(result.path);

		var notes = [];
		notes.push(result.tilesOnDisk.toLocaleString() + " tiles on disk (" + result.kind + (result.pattern ? ", " + result.pattern : "") + ")");
		if(result.hasManifest) {
			notes.push("Job manifest found" + (result.state ? ", last state: " + result.state : ""));
		} else if(result.hasMetadata) {
			notes.push("No job manifest: region and zooms come from metadata, check the source URL");
		} else {
			notes.push("No job manifest: region and zooms were inferred from the tiles on disk, check them");
		}
		if(result.invalidFiles) {
			notes.push(result.invalidFiles.toLocaleString() + " empty or broken tile files count as missing");
		}
		$("#dataset-notes").html(notes.map(function(n) { return $("<div></div>").text(n).html(); }).join("<br/>"));

		$("#resume-source-box").val(result.source || "");
		$("#resume-zoom-from-box").val(result.minZoom != null ? result.minZoom : 0);
		$("#resume-zoom-to-box").val(result.maxZoom != null ? result.maxZoom : 0);
		$("#resume-scale").text(result.outputScale + "x");
		M.updateTextFields();

		var zoomSelect = $("#coverage-zoom");
		zoomSelect.html("");
		var zooms = {};
		for(var i = 0; i < result.zoomsOnDisk.length; i++) {
			zooms[result.zoomsOnDisk[i].z] = true;
		}
		for(var z = Math.min(result.minZoom || 0, 24); z <= Math.max(result.maxZoom || 0, 0); z++) {
			zooms[z] = true;
		}
		Object.keys(zooms).map(Number).sort(function(a, b) { return a - b; }).forEach(function(z) {
			zoomSelect.append($("<option></option>").attr("value", z).text("zoom " + z));
		});
		zoomSelect.val(result.maxZoom);

		$("#retry-failed-button").toggle(result.failedCount > 0).text("Retry failed (" + (result.failedCount || 0).toLocaleString() + ")");

		draw.deleteAll();
		setRegion(result.region ? result.region.bounds : null, false);

		setCoverageDataset(result.ds);
		fitRegion();
		requestStats();
	}

	function fitRegion() {
		if(hasRegion()) {
			map.fitBounds(getBounds(), {padding: 40, animate: false});
		}
	}

	// after an edit settles (300 ms): recount against the opened folder and redraw the coverage
	function regionChanged() {
		removeGrid();
		if(regionTimer) {
			clearTimeout(regionTimer);
		}
		regionTimer = setTimeout(function() {
			regionTimer = null;
			if(dataset && $("#resume-sidebar").is(":visible")) {
				requestStats();
				refreshCoverage();
			}
		}, 300);
	}

	function scheduleStats() {
		if(statsTimer) {
			clearTimeout(statsTimer);
		}
		statsTimer = setTimeout(requestStats, 300);
	}

	function getResumeZooms() {
		var a = parseInt($("#resume-zoom-from-box").val());
		var b = parseInt($("#resume-zoom-to-box").val());
		if(isNaN(a) || isNaN(b)) {
			return null;
		}
		return [Math.min(a, b), Math.max(a, b)];
	}

	// one request in flight at a time; edits made meanwhile are sent as one follow-up request
	var statsInFlight = false;
	var statsQueued = false;
	var lastStatsMs = null;

	function requestStats() {
		if(!dataset) {
			return;
		}
		var zooms = getResumeZooms();
		if(zooms == null || !hasRegion()) {
			$("#zoom-table tbody").html("");
			$("#missing-summary").text(hasRegion() ? "Enter zoom levels" : "Draw a region to compare against");
			return;
		}
		if(statsInFlight) {
			statsQueued = true;
			return;
		}
		statsInFlight = true;

		var sent = performance.now();
		api("post", "/api/stats", {ds: dataset.ds, bounds: getBoundsArray(), polygon: getPolygon(), minZoom: zooms[0], maxZoom: zooms[1]}).always(function() {
			statsInFlight = false;
			if(statsQueued) {
				statsQueued = false;
				requestStats();
			}
		}).done(function(stats) {
			lastStatsMs = {server: stats.ms, roundTrip: Math.round(performance.now() - sent)};
			$("#region-bounds").text("Region " + getBoundsArray().map(function(v) { return v.toFixed(5); }).join(", "));
			var body = $("#zoom-table tbody");
			body.html("");
			for(var i = 0; i < stats.zooms.length; i++) {
				var row = stats.zooms[i];
				var tr = $("<tr></tr>");
				tr.append($("<td></td>").text(row.z));
				tr.append($("<td></td>").text(row.present.toLocaleString()));
				tr.append($("<td class='missing-cell'></td>").text(row.missing.toLocaleString()));
				tr.append($("<td></td>").text(row.total.toLocaleString()));
				if(row.missing == 0) {
					tr.addClass("complete-row");
				}
				tr.data("z", row.z);
				tr.click(function() {
					$("#coverage-zoom").val($(this).data("z"));
					refreshCoverage();
				});
				body.append(tr);
			}
			var t = stats.totals;
			if(t.missing == 0) {
				$("#missing-summary").text("Complete: all " + t.total.toLocaleString() + " tiles are on disk. Nothing to do.");
			} else {
				$("#missing-summary").text("Missing: " + t.missing.toLocaleString() + " of " + t.total.toLocaleString() + " tiles");
			}
			$("#resume-button").prop("disabled", t.missing == 0).text(t.missing == 0 ? "Nothing to resume" : "Resume (" + t.missing.toLocaleString() + " tiles)");
		}).fail(function(xhr) {
			M.toast({html: apiError(xhr, 'Could not count the tiles.'), displayLength: 5000});
		});
	}

	function resumeDataset(mode) {
		if(!dataset) {
			return;
		}
		var zooms = getResumeZooms();
		var source = $("#resume-source-box").val().trim();
		if(source == "" && mode != "failed") {
			M.toast({html: 'Enter the map tile source this folder was downloaded from.', displayLength: 5000});
			return;
		}
		if(zooms == null || !hasRegion()) {
			M.toast({html: 'Set the zoom levels and a region first.', displayLength: 5000});
			return;
		}
		startJob({
			mode: mode,
			ds: dataset.ds,
			source: source,
			minZoom: zooms[0],
			maxZoom: zooms[1],
			outputScale: dataset.outputScale || 1,
			threads: parseInt($("#resume-threads-box").val()) || 4,
			bounds: getBoundsArray(),
			polygon: getPolygon(),
		});
	}

	// ---------- coverage + imagery overlays ----------

	var coverageDs = null;

	function setCoverageDataset(ds) {
		coverageDs = ds;
		lastCoverageVersion = -1;
		refreshCoverage();
	}

	function clearCoverage() {
		coverageDs = null;
		removeLayer("ds-coverage");
		removeLayer("ds-imagery");
	}

	// keep the overlays under the region rectangle so it stays visible and editable
	function firstDrawLayer() {
		if(map.getLayer("region-fill")) {
			return "region-fill";
		}
		var layers = map.getStyle().layers;
		for(var i = 0; i < layers.length; i++) {
			if(layers[i].id.indexOf("gl-draw") == 0) {
				return layers[i].id;
			}
		}
		return undefined;
	}

	function refreshCoverage() {

		if(!coverageDs || !mapReady) {
			return;
		}

		var version = Date.now();
		var showImagery = $("#show-imagery-checkbox").is(":checked");
		var showCoverage = $("#show-coverage-checkbox").is(":checked");

		removeLayer("ds-imagery");
		if(showImagery) {
			map.addLayer({
				id: "ds-imagery",
				type: "raster",
				source: {
					type: "raster",
					tiles: [location.origin + "/api/tiles/{z}/{x}/{y}?ds=" + coverageDs + "&v=" + version],
					tileSize: 256,
					maxzoom: (dataset && dataset.maxZoom) || 18,
				},
			}, firstDrawLayer());
		}

		removeLayer("ds-coverage");
		if(showCoverage) {
			var dz = parseInt($("#coverage-zoom").val());
			if(isNaN(dz)) {
				dz = getResumeZooms() ? getResumeZooms()[1] : getMaxZoom();
			}
			var url = location.origin + "/api/coverage/{z}/{x}/{y}.png?ds=" + coverageDs + "&dz=" + dz + "&v=" + version;
			if(hasRegion()) {
				url += "&bounds=" + getBoundsArray().join(",");
			}
			map.addLayer({
				id: "ds-coverage",
				type: "raster",
				source: {
					type: "raster",
					tiles: [url],
					tileSize: 256,
				},
				paint: {"raster-opacity": 0.85},
			}, firstDrawLayer());
		}
	}

	initializeMaterialize();
	initializeSources();
	initializeMap();
	initializeSearch();
	initializeRectangleTool();
	initializeGridPreview();
	initializeMoreOptions();
	initializeDownloader();
	initializeResume();
});
