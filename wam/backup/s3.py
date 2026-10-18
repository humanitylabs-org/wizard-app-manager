"""A small S3 client on the standard library: SigV4 signing, PUT, multipart upload, GET (with Range), HEAD,
ListObjectsV2 and DELETE. Path-style addressing (`<endpoint>/<bucket>/<key>`), which Cloudflare R2, MinIO, Backblaze B2
and AWS S3 all accept. Bodies stream from disk; a multipart part is read twice (hash, then send) rather than held.
Credentials are never logged; error messages carry the S3 error code only.
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import io
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

PART = 64 << 20
EMPTY = hashlib.sha256(b"").hexdigest()


class S3Error(RuntimeError):
    def __init__(self, msg: str, status: int = 0, code: str = "") -> None:
        super().__init__(msg)
        self.status, self.code = status, code


def _q(s: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(s, safe=safe)


class _Region:
    """A file region as a stream: `size` bytes of `path` from `offset`."""

    def __init__(self, path: Path, offset: int, size: int) -> None:
        self.f = open(path, "rb")
        self.f.seek(offset)
        self.left = size

    def read(self, n: int = -1) -> bytes:
        if self.left <= 0:
            return b""
        n = self.left if n is None or n < 0 else min(n, self.left)
        b = self.f.read(n)
        self.left -= len(b)
        return b

    def close(self) -> None:
        self.f.close()


def _sha_region(path: Path, offset: int, size: int) -> str:
    h = hashlib.sha256()
    r = _Region(path, offset, size)
    try:
        while b := r.read(1 << 20):
            h.update(b)
    finally:
        r.close()
    return h.hexdigest()


class S3:
    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str = "auto",
                 part_size: int | None = None, timeout: int = 600) -> None:
        u = urllib.parse.urlparse(endpoint if "://" in endpoint else "https://" + endpoint)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise S3Error(f"bad S3 endpoint {endpoint!r}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,62}", bucket or ""):
            raise S3Error(f"bad bucket name {bucket!r}")
        self.scheme, self.host, self.port = u.scheme, u.hostname, u.port
        self.base = (u.path or "").rstrip("/")
        self.bucket, self.region = bucket, region or "auto"
        self._ak, self._sk = access_key, secret_key
        self.part_size = int(part_size or os.environ.get("WAM_BACKUP_S3_PART_SIZE") or PART)
        self.timeout = timeout

    # ------------------------------------------------------------ signing
    def _hostport(self) -> str:
        default = 443 if self.scheme == "https" else 80
        return self.host if not self.port or self.port == default else f"{self.host}:{self.port}"

    def _sign(self, method: str, path: str, query: dict, headers: dict, payload_hash: str) -> dict:
        t = time.gmtime()
        amz = time.strftime("%Y%m%dT%H%M%SZ", t)
        day = amz[:8]
        h = {k.lower(): str(v).strip() for k, v in headers.items()}
        h["host"] = self._hostport()
        h["x-amz-date"] = amz
        h["x-amz-content-sha256"] = payload_hash
        signed = ";".join(sorted(h))
        cq = "&".join(f"{_q(k)}={_q(str(v))}" for k, v in sorted(query.items()))
        creq = "\n".join([method, _q(path, "/-_.~"), cq, "".join(f"{k}:{h[k]}\n" for k in sorted(h)), signed, payload_hash])
        scope = f"{day}/{self.region}/s3/aws4_request"
        sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(creq.encode()).hexdigest()])
        k = ("AWS4" + self._sk).encode()
        for part in (day, self.region, "s3", "aws4_request"):
            k = hmac.new(k, part.encode(), hashlib.sha256).digest()
        sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
        h["authorization"] = f"AWS4-HMAC-SHA256 Credential={self._ak}/{scope}, SignedHeaders={signed}, Signature={sig}"
        return h

    def _path(self, key: str = "") -> str:
        return f"{self.base}/{self.bucket}" + (f"/{key}" if key else "")

    def _conn(self) -> http.client.HTTPConnection:
        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=self.timeout)

    def request(self, method: str, key: str = "", query: dict | None = None, headers: dict | None = None,
                body=None, payload_hash: str = EMPTY, length: int | None = None, stream: bool = False):
        query = query or {}
        headers = dict(headers or {})
        if length is not None:
            headers["content-length"] = str(length)
        path = self._path(key)
        h = self._sign(method, path, query, headers, payload_hash)
        url = _q(path, "/-_.~") + ("?" + "&".join(f"{_q(k)}={_q(str(v))}" for k, v in sorted(query.items())) if query else "")
        last = None
        for attempt in range(4):
            c = self._conn()
            try:
                c.putrequest(method, url, skip_host=True, skip_accept_encoding=True)
                for k, v in h.items():
                    c.putheader(k, v)
                c.endheaders()
                if body is not None:
                    while chunk := body.read(1 << 20):
                        c.send(chunk)
                resp = c.getresponse()
                if resp.status >= 400:
                    data = resp.read()
                    c.close()
                    code = _xml_text(data, "Code") or str(resp.status)
                    if resp.status >= 500 and attempt < 3 and body is None:
                        last = S3Error(f"S3 {method} -> {resp.status} {code}", resp.status, code)
                        time.sleep(2 ** attempt)
                        continue
                    raise S3Error(f"S3 {method} {key or self.bucket} -> {resp.status} {code}", resp.status, code)
                if stream:
                    return resp, c
                data = resp.read()
                hdrs = {k.lower(): v for k, v in resp.getheaders()}
                c.close()
                return resp.status, hdrs, data
            except (OSError, http.client.HTTPException) as e:
                c.close()
                last = S3Error(f"S3 {method} failed: {type(e).__name__}")
                if body is not None or attempt == 3:
                    raise last from None
                time.sleep(2 ** attempt)
        raise last or S3Error("S3 request failed")

    # ------------------------------------------------------------ objects
    def put_file(self, key: str, path: Path, progress=None) -> dict:
        size = Path(path).stat().st_size
        if size <= self.part_size:
            r = _Region(path, 0, size)
            try:
                _, h, _ = self.request("PUT", key, body=r, payload_hash=_sha_region(path, 0, size), length=size,
                                       headers={"content-type": "application/octet-stream"})
            finally:
                r.close()
            return {"key": key, "bytes": size, "etag": h.get("etag"), "parts": 1}
        _, _, data = self.request("POST", key, {"uploads": ""}, headers={"content-type": "application/octet-stream"})
        upload = _xml_text(data, "UploadId")
        if not upload:
            raise S3Error("S3 did not start a multipart upload")
        parts = []
        try:
            n, off = 1, 0
            while off < size:
                ln = min(self.part_size, size - off)
                sha = _sha_region(path, off, ln)
                for attempt in range(3):
                    r = _Region(path, off, ln)
                    try:
                        _, h, _ = self.request("PUT", key, {"partNumber": n, "uploadId": upload}, body=r,
                                               payload_hash=sha, length=ln)
                        break
                    except S3Error:
                        if attempt == 2:
                            raise
                        time.sleep(2 ** attempt)
                    finally:
                        r.close()
                parts.append((n, h.get("etag", "")))
                off += ln
                if progress:
                    progress(f"uploaded {off >> 20} of {size >> 20} MiB")
                n += 1
            xml = "<CompleteMultipartUpload>" + "".join(
                f"<Part><PartNumber>{i}</PartNumber><ETag>{e}</ETag></Part>" for i, e in parts) + "</CompleteMultipartUpload>"
            body = xml.encode()
            _, _, data = self.request("POST", key, {"uploadId": upload}, body=io.BytesIO(body),
                                      payload_hash=hashlib.sha256(body).hexdigest(), length=len(body),
                                      headers={"content-type": "application/xml"})
            if b"<Error>" in data:
                raise S3Error(f"S3 multipart complete failed: {_xml_text(data, 'Code')}")
        except BaseException:
            try:
                self.request("DELETE", key, {"uploadId": upload})
            except S3Error:
                pass
            raise
        return {"key": key, "bytes": size, "parts": len(parts)}

    def head(self, key: str) -> dict:
        _, h, _ = self.request("HEAD", key)
        return {"bytes": int(h.get("content-length", -1)), "etag": h.get("etag")}

    def get_range(self, key: str, start: int, end: int) -> bytes:
        _, _, data = self.request("GET", key, headers={"range": f"bytes={start}-{end}"})
        return data

    def open(self, key: str):
        """A streaming GET: a file-like object (close it)."""
        resp, conn = self.request("GET", key, stream=True)
        return _Body(resp, conn)

    def delete(self, key: str) -> None:
        self.request("DELETE", key)

    def list(self, prefix: str = "") -> list[dict]:
        out, token = [], None
        while True:
            q = {"list-type": "2", "prefix": prefix}
            if token:
                q["continuation-token"] = token
            _, _, data = self.request("GET", "", q)
            root = ET.fromstring(data)
            ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
            for c in root.findall(f"{ns}Contents"):
                out.append({"key": c.findtext(f"{ns}Key"), "bytes": int(c.findtext(f"{ns}Size") or 0),
                            "modified": c.findtext(f"{ns}LastModified")})
            if (root.findtext(f"{ns}IsTruncated") or "").lower() == "true":
                token = root.findtext(f"{ns}NextContinuationToken")
                if not token:
                    break
            else:
                break
        return out


class _Body:
    def __init__(self, resp, conn) -> None:
        self.resp, self.conn = resp, conn

    def read(self, n: int = -1) -> bytes:
        return self.resp.read() if n is None or n < 0 else self.resp.read(n)

    def close(self) -> None:
        self.resp.close()
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        self.close()


def _xml_text(data: bytes, tag: str) -> str | None:
    m = re.search(rb"<" + tag.encode() + rb">([^<]*)</" + tag.encode() + rb">", data or b"")
    return m.group(1).decode() if m else None
