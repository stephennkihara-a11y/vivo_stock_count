"""PWA shell, manifest, and service worker.

The PWA is installable at /vivo-count/pwa. The service worker is served
from the same URL prefix so it can claim that scope and cache the shell
+ static assets for the 60-minute offline window (AC #8).
"""
import os

from odoo import http
from odoo.http import request
from odoo.modules.module import get_module_path
from odoo.tools import file_open


def _serve_static(relpath, mimetype):
    path = os.path.join(get_module_path("vivo_stock_count"), "static", "pwa", relpath)
    with file_open(path, "rb") as f:
        data = f.read()
    headers = [("Content-Type", mimetype), ("Cache-Control", "no-cache")]
    return request.make_response(data, headers=headers)


class VivoCountPWA(http.Controller):

    @http.route(
        "/vivo-count/pwa", auth="user", type="http", website=False, csrf=False
    )
    def pwa_shell(self, **kw):
        return _serve_static("index.html", "text/html; charset=utf-8")

    @http.route(
        "/vivo-count/pwa/manifest.webmanifest",
        auth="user",
        type="http",
        website=False,
        csrf=False,
    )
    def pwa_manifest(self, **kw):
        return _serve_static("manifest.webmanifest", "application/manifest+json")

    @http.route(
        "/vivo-count/pwa/sw.js",
        auth="user",
        type="http",
        website=False,
        csrf=False,
    )
    def pwa_sw(self, **kw):
        # Service workers must be served from inside their intended scope.
        return _serve_static("sw.js", "application/javascript")
