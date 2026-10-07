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

Streaming
---------
Requests use ``stream=True``. Besides avoiding buffering whole images in
memory, this changes curl's timeout from a hard *total transfer* cap
(``CURLOPT_TIMEOUT``) to a connect timeout plus a low-speed timeout
(``CURLOPT_LOW_SPEED_LIMIT/TIME``): a download that keeps receiving data,
however slowly, is never killed just for being large/slow, while a stalled
connection still aborts after the configured number of seconds.
"""

import email
import http.client
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


class _CurlStreamReader:
    """File-like adapter over curl_cffi's chunk iterator.

    Implements the small interface ``mechanize.closeable_response`` needs:
    ``read``/``readline``/``readlines``/``__iter__``/``close``. curl errors
    raised while pulling chunks are converted to ``URLError`` so the
    application's existing network-error retry logic handles them.
    """

    def __init__(self, curl_response):
        self._r = curl_response
        self._chunks = iter(curl_response.iter_content())
        self._buf = b""
        self._eof = False
        self._closed = False

    def _pull_chunk(self):
        """Fetch the next decoded chunk; b"" means EOF. URLError on failure."""
        if self._eof:
            return b""
        try:
            chunk = next(self._chunks)
        except StopIteration:
            chunk = b""
        except Exception as ex:  # curl_cffi CurlError/RequestException
            self._eof = True
            raise URLError(str(ex)) from ex
        if chunk == b"":
            self._eof = True
        return chunk

    def read(self, size=-1):
        if self._closed:
            raise ValueError("read of closed file")
        if size is None or size < 0:
            parts = [self._buf]
            self._buf = b""
            while not self._eof:
                parts.append(self._pull_chunk())
            return b"".join(parts)

        while len(self._buf) < size and not self._eof:
            chunk = self._pull_chunk()
            if chunk:
                self._buf += chunk
        data, self._buf = self._buf[:size], self._buf[size:]
        return data

    def readline(self, size=-1):
        if self._closed:
            raise ValueError("readline of closed file")
        while b"\n" not in self._buf and not self._eof:
            chunk = self._pull_chunk()
            if not chunk:
                break
            self._buf += chunk
        idx = self._buf.find(b"\n")
        if idx >= 0:
            end = idx + 1
            line, self._buf = self._buf[:end], self._buf[end:]
        else:
            line, self._buf = self._buf, b""
        if size is not None and 0 <= size < len(line):
            self._buf = line[size:] + self._buf
            line = line[:size]
        return line

    def readlines(self, hint=-1):
        lines = []
        total = 0
        while True:
            line = self.readline()
            if not line:
                break
            lines.append(line)
            total += len(line)
            if hint and hint > 0 and total >= hint:
                break
        return lines

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._eof = True
        try:
            # Signals the background curl thread to stop and releases the
            # curl handle (safe to call even if the stream was fully consumed).
            self._r.close()
        except Exception:
            pass


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
    def _build_message(curl_response):
        """Rebuild an RFC822/HTTPMessage header object from curl_cffi output."""
        multi_items = getattr(curl_response.headers, "multi_items", None)
        if callable(multi_items):
            header_pairs = list(multi_items())
        else:
            header_pairs = list(curl_response.headers.items())
        raw_headers = "\r\n".join(f"{name}: {value}" for name, value in header_pairs)
        message = email.message_from_string(
            raw_headers, _class=http.client.HTTPMessage
        )

        # libcurl has already decompressed the stream; leaving
        # Content-Encoding in place would make mechanize's gzip processor
        # decode it twice. Content-Length is kept as sent by the server.
        del message["Content-Encoding"]
        return message

    def _do_open(self, req):
        config = self._config
        proxies = config.proxy if config.useProxy else None

        timeout = config.timeout
        request_timeout = getattr(req, "timeout", None)
        if isinstance(request_timeout, (int, float)) and not isinstance(
            request_timeout, bool
        ) and request_timeout > 0:
            timeout = request_timeout

        headers = self._build_headers(req)

        try:
            # stream=True: headers arrive (and header-stage errors raise)
            # before this returns; the body is pulled lazily via the reader.
            # The scalar timeout becomes connect-timeout + low-speed-time,
            # i.e. abort only when no data arrives for `timeout` seconds.
            curl_response = curl_requests.request(
                method=req.get_method(),
                url=req.get_full_url(),
                data=req.get_data(),
                headers=headers,
                proxies=proxies,
                timeout=timeout,
                verify=bool(config.enableSSLVerification),
                impersonate=self._impersonate,
                stream=True,
                # Let mechanize's redirect/cookie processors handle 3xx so
                # Set-Cookie on redirect responses reaches the cookie jar.
                allow_redirects=False,
            )
        except Exception as ex:
            # Map transport failures to URLError: open_with_retry() treats
            # these as retryable network errors, matching old behaviour.
            self._logger.debug("curl_cffi request failed: %r", ex)
            raise URLError(str(ex)) from ex

        message = self._build_message(curl_response)
        reason = getattr(curl_response, "reason", None) or "OK"

        response = closeable_response(
            _CurlStreamReader(curl_response),
            message,
            req.get_full_url(),
            curl_response.status_code,
            reason,
            None,
        )

        # HTTPErrorProcessor (process_response) raises HTTPError for >=400 and
        # HTTPRedirectProcessor handles 3xx, exactly as with the old handler.
        return response
