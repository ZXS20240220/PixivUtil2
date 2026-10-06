# -*- coding: utf-8 -*-
"""curl_cffi backed HTTP(S) transport for mechanize.

Background
----------
www.pixiv.net / *.fanbox.cc sit behind Cloudflare bot management, which
fingerprints the TLS ClientHello (JA3/JA4) and the HTTP/2 SETTINGS frame in
addition to plain HTTP headers. mechanize talks HTTP/1.1 through Python's
``ssl`` module, so its fingerprint is trivially recognisable and the edge
answers with ``HTTP 403`` and ``cf-mitigated: challenge`` even when a valid
``PHPSESSID`` cookie is present. ``curl_cffi`` drives libcurl with a real
browser fingerprint (``impersonate=...``) and passes the same checks.

This module installs a mechanize transport handler that sends every
http/https request through curl_cffi while leaving mechanize's cookie jar,
redirect processor, error processor and retry logic untouched, so the rest
of the application keeps working unchanged.
"""

import email
import http.client
import io
from numbers import Real
from urllib.error import URLError

import curl_cffi
from curl_cffi import requests as curl_requests
from mechanize._response import closeable_response
from mechanize._urllib2_fork import BaseHandler

import common.PixivHelper as PixivHelper


# Headers managed by libcurl / the TLS stack; never forward mechanize's
# HTTP/1.1-era values (they would downgrade or corrupt the impersonation).
_HOP_BY_HOP = ("connection", "host", "proxy-authorization", "te",
               "trailer", "transfer-encoding", "upgrade", "keep-alive")

# Default profile; can be overridden with [Network] userAgentImpersonation.
# The "firefox" alias always maps to the newest Firefox profile bundled with
# the installed curl_cffi release.
_DEFAULT_IMPERSONATE = "firefox"


class CurlCffiHandler(BaseHandler):
    """Mechanize ``http_open``/``https_open`` handler backed by curl_cffi."""

    # Default mechanize HTTP(S) handlers use handler_order = 500; the opener
    # calls handlers in ascending order and takes the first response, so a
    # smaller value makes this handler win.
    handler_order = 50

    def __init__(self, config):
        self._config = config
        impersonate = getattr(config, "userAgentImpersonation", "") or ""
        self._impersonate = impersonate.strip() or _DEFAULT_IMPERSONATE
        self._logger = PixivHelper.get_logger()
        self._logger.info(
            "HTTP transport: curl_cffi %s (impersonate=%s, proxy=%s)",
            getattr(curl_cffi, "__version__", "?"),
            self._impersonate,
            config.proxyAddress if config.useProxy else "disabled",
        )

    def __copy__(self):
        # Browser.__copy__ clones every handler; keep the config reference.
        return self.__class__(self._config)

    # ------------------------------------------------------------------ open
    def http_open(self, req):
        return self._do_open(req)

    def https_open(self, req):
        return self._do_open(req)

    # ------------------------------------------------------------- internals
    def _build_headers(self, req):
        """Replicate mechanize AbstractHTTPHandler.do_request_ header setup."""
        headers = {}

        # Browser-level default headers (User-agent), as do_request_ does.
        parent = getattr(self, "parent", None)
        for name, value in getattr(parent, "addheaders", []) or []:
            if not req.has_header(name.capitalize()):
                headers[name] = value

        # Both normal and unredirected (Cookie, Referer, Content-type, ...).
        for name, value in req.header_items():
            if name.lower() in _HOP_BY_HOP:
                continue
            headers[name] = value

        if req.has_data():
            if not req.has_header("Content-type"):
                headers["Content-type"] = "application/x-www-form-urlencoded"
            data = req.get_data()
            if not req.has_header("Content-length") and hasattr(data, "__len__"):
                headers["Content-length"] = str(len(data))

        return headers

    @staticmethod
    def _build_message(curl_response, body_length):
        """Rebuild an RFC822/HTTPMessage header object from curl_cffi output."""
        header_pairs = []
        multi_items = getattr(curl_response.headers, "multi_items", None)
        if callable(multi_items):
            header_pairs = list(multi_items())
        else:
            header_pairs = list(curl_response.headers.items())
        raw_headers = "\r\n".join(f"{name}: {value}" for name, value in header_pairs)
        message = email.message_from_string(
            raw_headers, _class=http.client.HTTPMessage
        )

        # libcurl has already decompressed the body; leaving Content-Encoding
        # in place would make mechanize's gzip processor decode it twice.
        del message["Content-Encoding"]
        message["Content-Length"] = str(body_length)
        return message

    def _do_open(self, req):
        config = self._config
        proxies = config.proxy if config.useProxy else None

        timeout = config.timeout
        request_timeout = getattr(req, "timeout", None)
        if isinstance(request_timeout, Real) and request_timeout > 0:
            timeout = request_timeout

        headers = self._build_headers(req)

        try:
            curl_response = curl_requests.request(
                method=req.get_method(),
                url=req.get_full_url(),
                data=req.get_data(),
                headers=headers,
                proxies=proxies,
                timeout=timeout,
                verify=bool(config.enableSSLVerification),
                impersonate=self._impersonate,
                # Let mechanize's redirect/cookie processors handle 3xx so
                # Set-Cookie on redirect responses reaches the cookie jar.
                allow_redirects=False,
            )
        except Exception as ex:
            # Map transport failures to URLError: open_with_retry() treats
            # these as retryable network errors, matching old behaviour.
            self._logger.debug("curl_cffi request failed: %r", ex)
            raise URLError(str(ex)) from ex

        body = curl_response.content or b""
        message = self._build_message(curl_response, len(body))
        reason = getattr(curl_response, "reason", None) or "OK"

        # BytesIO provides read/readline/readlines, which is everything
        # closeable_response and the seek wrapper need.
        response = closeable_response(
            io.BytesIO(body),
            message,
            req.get_full_url(),
            curl_response.status_code,
            reason,
            None,
        )

        # HTTPErrorProcessor (process_response) raises HTTPError for >=400 and
        # HTTPRedirectProcessor handles 3xx, exactly as with the old handler.
        return response
