# Map preview of the tile source chosen in the UI.
#
# The page registers the URL template it wants to preview (POST /api/preview/source, JSON only, so a
# cross-site page cannot do it) and then asks for tiles by id. The server fills the template with the
# same Utils.qualifyURL and request headers the downloader uses, so the preview shows what a download
# would get, and the browser needs no CORS support from the tile provider.
#
# Not an open proxy:
#  - only a registered template is fetched, filled with integer z/x/y; the last few templates are kept
#  - http/https only, no placeholders in the host name, at least {x}{y}{z} or {quad}
#  - the host is resolved once and every address checked; the connection goes to that checked address
#    (so a DNS answer cannot change between the check and the fetch)
#  - public addresses are allowed; loopback and private (LAN) addresses only when the browser itself is on
#    this computer (it could reach them directly anyway); link-local (cloud metadata), multicast,
#    unspecified and reserved addresses never
#  - no redirects, image bodies only, 4 MB and 10 s limits
import hashlib
import http.client
import ipaddress
import socket
import threading
import urllib.parse
from collections import OrderedDict

from utils import Utils, HEADERS, SSL_CONTEXT

MAX_TEMPLATES = 4
MAX_TEMPLATE_LENGTH = 2048
MAX_BODY = 4 * 1024 * 1024
TIMEOUT = 10

_lock = threading.Lock()
_templates = OrderedDict()


class PreviewRefused(Exception):
	pass


def validateTemplate(template):
	if not isinstance(template, str) or not template.strip():
		raise ValueError("Enter a tile URL to preview")
	template = template.strip()
	if len(template) > MAX_TEMPLATE_LENGTH:
		raise ValueError("Tile URL is too long")
	parts = urllib.parse.urlsplit(template)
	if parts.scheme not in ("http", "https") or not parts.hostname:
		raise ValueError("Preview needs an http:// or https:// tile URL")
	if "{" in parts.netloc or "}" in parts.netloc:
		raise ValueError("Preview does not support placeholders in the host name")
	if not (("{x}" in template and "{y}" in template and "{z}" in template) or "{quad}" in template):
		raise ValueError("The tile URL has no {x}, {y} and {z} (or {quad}) placeholders")
	return template


def register(template):
	template = validateTemplate(template)
	key = hashlib.sha1(template.encode("utf-8")).hexdigest()[:12]
	with _lock:
		_templates[key] = {"template": template, "ok": 0, "failed": 0, "lastError": None}
		_templates.move_to_end(key)
		while len(_templates) > MAX_TEMPLATES:
			_templates.popitem(last=False)
	return key


def status(key):
	with _lock:
		entry = _templates.get(key)
		if entry is None:
			return None
		return {"ok": entry["ok"], "failed": entry["failed"], "lastError": entry["lastError"]}


def _count(key, ok, error=None):
	with _lock:
		entry = _templates.get(key)
		if entry is None:
			return
		if ok:
			entry["ok"] += 1
		else:
			entry["failed"] += 1
			entry["lastError"] = error


def _allowedAddress(ip, clientIsLocal):
	if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
		return False
	# Python also counts ::1 as reserved, so loopback is decided first
	if ip.is_loopback:
		return clientIsLocal
	if ip.is_reserved:
		return False
	if ip.is_global:
		return True
	return clientIsLocal and (ip.is_loopback or ip.is_private)


def checkedAddress(host, port, clientIsLocal):
	try:
		infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
	except socket.gaierror:
		raise PreviewRefused("host not found")
	addresses = []
	for info in infos:
		ip = ipaddress.ip_address(info[4][0].split("%")[0])
		if getattr(ip, "ipv4_mapped", None):
			ip = ip.ipv4_mapped
		if not _allowedAddress(ip, clientIsLocal):
			raise PreviewRefused("address not allowed for preview")
		addresses.append(str(ip))
	if not addresses:
		raise PreviewRefused("host not found")
	return addresses[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
	# connects to an already checked address but keeps the host name for SNI and the Host header

	def __init__(self, host, port, address, **kwargs):
		http.client.HTTPSConnection.__init__(self, host, port, **kwargs)
		self._address = address

	def connect(self):
		sock = socket.create_connection((self._address, self.port), self.timeout)
		self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def fetch(key, x, y, z, clientIsLocal):
	# returns (bytes, mime) or (None, None); failures are counted for /api/preview/status
	with _lock:
		entry = _templates.get(key)
		template = entry["template"] if entry else None
	if template is None:
		return None, None
	url = Utils.qualifyURL(template, x, y, z)
	parts = urllib.parse.urlsplit(url)
	port = parts.port or (443 if parts.scheme == "https" else 80)
	path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
	connection = None
	try:
		address = checkedAddress(parts.hostname, port, clientIsLocal)
		if parts.scheme == "https":
			connection = _PinnedHTTPSConnection(parts.hostname, port, address, timeout=TIMEOUT, context=SSL_CONTEXT)
		else:
			connection = http.client.HTTPConnection(address, port, timeout=TIMEOUT)
		headers = dict(HEADERS)
		headers["Host"] = parts.netloc.rsplit("@", 1)[-1]
		connection.request("GET", path, headers=headers)
		response = connection.getresponse()
		body = response.read(MAX_BODY + 1)
		if response.status != 200:
			raise PreviewRefused("HTTP " + str(response.status))
		if len(body) > MAX_BODY:
			raise PreviewRefused("tile too large")
		mime = Utils.sniffImageType(body) if body else None
		if not mime or not mime.startswith("image/"):
			raise PreviewRefused("not an image")
		_count(key, True)
		return body, mime
	except PreviewRefused as e:
		_count(key, False, str(e))
	except (OSError, http.client.HTTPException, ValueError) as e:
		_count(key, False, "network error: " + type(e).__name__)
	finally:
		if connection is not None:
			connection.close()
	return None, None
