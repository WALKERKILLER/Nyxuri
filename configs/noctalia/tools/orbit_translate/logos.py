"""Optional public provider logos; bounded vector-only cache, no user data."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET

from .config import CATALOG_PROVIDER_ID


LOGO_ROOT = "https://models.dev/logos/"
MAX_SVG_BYTES = 64 * 1024
MAX_CACHE_BYTES = 4 * MAX_SVG_BYTES
LOGO_TTL_SECONDS = 7 * 24 * 60 * 60
_SVG_NS = "http://www.w3.org/2000/svg"
# GdkPixbuf's SVG sniffing requires an unprefixed <svg> root.
ET.register_namespace("", _SVG_NS)
_TAGS = frozenset({"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "defs", "clipPath", "mask", "linearGradient", "radialGradient", "stop", "use"})
_ATTRS = frozenset({"viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "d", "points", "id", "href", "fill", "fill-rule", "fill-opacity", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "stroke-miterlimit", "stroke-dasharray", "stroke-dashoffset", "stroke-opacity", "opacity", "color", "transform", "style", "clip-path", "clip-rule", "clipPathUnits", "mask", "maskUnits", "maskContentUnits", "gradientUnits", "gradientTransform", "offset", "stop-color", "stop-opacity", "spreadMethod", "preserveAspectRatio", "shape-rendering", "vector-effect", "version"})
_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)


def sanitize_svg(data: bytes) -> bytes | None:
    """Reject active/external SVG content before it reaches the image decoder."""
    if not data or len(data) > MAX_SVG_BYTES:
        return None
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        return None
    try:
        root = ET.fromstring(data)
        if root.tag not in {"svg", f"{{{_SVG_NS}}}svg"}:
            return None
        stack = [(root, 0)]
        count = 0
        while stack:
            node, depth = stack.pop()
            count += 1
            if count > 2048 or depth > 32:
                return None
            tag = node.tag.removeprefix(f"{{{_SVG_NS}}}")
            if tag not in _TAGS:
                return None
            for key, value in node.attrib.items():
                key = key.removeprefix("{http://www.w3.org/1999/xlink}")
                if key not in _ATTRS or any(ord(char) < 32 and char not in "\t\r\n" for char in value):
                    return None
                if key == "href" and not re.fullmatch(r"#[A-Za-z_][A-Za-z0-9_.:-]*", value):
                    return None
                if any(token in value.lower() for token in ("://", "data:", "file:", "@import", "\\")):
                    return None
                if any(not match.group(2).strip().startswith("#") for match in _URL.finditer(value)):
                    return None
                if "url" in value.lower() and not _URL.search(value):
                    return None
            # Logos contain geometry only. Normalize whitespace for default detection.
            if node.text and node.text.strip():
                return None
            node.text = None
            node.tail = None
            node.attrib = dict(sorted(node.attrib.items()))
            stack.extend((child, depth + 1) for child in node)
        result = ET.tostring(root, encoding="utf-8")
        return result if len(result) <= MAX_SVG_BYTES else None
    except (ET.ParseError, ValueError, UnicodeError, RecursionError):
        return None


def colored_svg(data: bytes, color: str) -> bytes | None:
    safe = sanitize_svg(data)
    if safe is None or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        return None
    return safe.replace(b"currentColor", color.encode("ascii"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError("Logo redirect refused")


def _download(provider_id: str) -> bytes | None:
    if not CATALOG_PROVIDER_ID.fullmatch(provider_id):
        return None
    url = f"{LOGO_ROOT}{provider_id}.svg"
    request = urllib.request.Request(url, headers={"Accept": "image/svg+xml", "User-Agent": "Orbit-Translate/1.0 (public provider logo)"})
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=3) as response:
            if response.geturl() != url:
                return None
            return sanitize_svg(response.read(MAX_SVG_BYTES + 1))
    except Exception:
        # Optional branding failures never enter translation diagnostics.
        return None


class ProviderLogoStore:
    def __init__(self, cache_dir: str | Path | None = None):
        cache_home = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        self.cache_dir = Path(cache_dir) if cache_dir is not None else cache_home / "orbit-translate/provider-logos-v1"

    def _read(self, provider_id: str) -> tuple[float, bytes | None] | None:
        if not CATALOG_PROVIDER_ID.fullmatch(provider_id):
            return None
        try:
            path = self.cache_dir / f"{provider_id}.json"
            with path.open("rb") as handle:
                data = handle.read(MAX_CACHE_BYTES + 1)
            if len(data) > MAX_CACHE_BYTES:
                return None
            raw = json.loads(data)
            timestamp = raw["fetched_at"]
            if raw["source"] != f"{LOGO_ROOT}{provider_id}.svg" or raw["version"] != 1:
                return None
            if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp < 0 or timestamp > time.time() + 60:
                return None
            svg = raw["svg"]
            if not isinstance(svg, str):
                return None
            safe = sanitize_svg(svg.encode("utf-8")) if svg else None
            if svg and safe is None:
                return None
            return timestamp, safe
        except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError):
            return None

    def cached(self, provider_id: str) -> bytes | None:
        value = self._read(provider_id)
        return value[1] if value else None

    def _write(self, provider_id: str, svg: bytes | None) -> None:
        from .catalog import _CACHE_LICENSE_NOTICE

        payload = {"version": 1, "source": f"{LOGO_ROOT}{provider_id}.svg", "fetched_at": time.time(), "svg": svg.decode("utf-8") if svg else "", "license_notice": _CACHE_LICENSE_NOTICE}
        temporary: str | None = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.cache_dir, prefix=".logo-", delete=False) as handle:
                temporary = handle.name
                handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            os.replace(temporary, self.cache_dir / f"{provider_id}.json")
        except OSError:
            pass
        finally:
            if temporary:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass

    def load(self, provider_id: str) -> bytes | None:
        """Call from a bounded daemon worker; cached misses also avoid HTTP."""
        if not CATALOG_PROVIDER_ID.fullmatch(provider_id):
            return None
        previous = self._read(provider_id)
        if previous and time.time() - previous[0] < LOGO_TTL_SECONDS:
            return previous[1]
        if provider_id == "default":
            svg = _download(provider_id)
        else:
            default = self.load("default")
            if default is None:
                return previous[1] if previous else None
            svg = _download(provider_id)
            if svg is not None and svg == default:
                self._write(provider_id, None)
                return None
        if svg is None:
            return previous[1] if previous else None
        self._write(provider_id, svg)
        return svg
