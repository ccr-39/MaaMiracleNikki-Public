"""Anonymous HTTPS access, restricted to the official GitHub release assets."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import json
from pathlib import Path
import re
import shutil
import tempfile
from threading import Event
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from updates.version import Version, current_version

REPOSITORY = "ccr-39/MaaMiracleNikki-Public"
API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
PREFIX = f"https://github.com/{REPOSITORY}/releases/download/"
MAX_PACKAGE = 2 * 1024**3


class UpdateError(RuntimeError):
    pass


class Cancelled(UpdateError):
    pass


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled("已取消下载")


def official_asset(url):
    parsed = urlsplit(url)
    return (url.startswith(PREFIX) and parsed.scheme == "https" and parsed.netloc == "github.com"
            and not parsed.query and not parsed.fragment)


class RestrictedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        allowed = official_asset(newurl) or (parsed.scheme == "https" and parsed.netloc in {
            "release-assets.githubusercontent.com", "objects.githubusercontent.com"})
        # The API endpoint is never redirected to an asset or another repository.
        if req.full_url == API or not allowed:
            raise UpdateError("GitHub 返回了不受信任的下载地址")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Transport:
    def __init__(self):
        self.opener = build_opener(RestrictedRedirect())

    def open(self, url):
        if url != API and not official_asset(url):
            raise UpdateError("仅允许官方仓库的 Release 资产")
        request = Request(url, headers={"User-Agent": "MaaMiracleNikki-Updater",
                                       "Accept": "application/vnd.github+json" if url == API else "application/octet-stream"})
        return self.opener.open(request, timeout=10)


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    digest: str = ""


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    package: Asset | None
    checksum: Asset | None


def parse_release(data):
    if not isinstance(data, dict):
        raise UpdateError("GitHub 版本信息格式无效")
    try:
        version = Version(data["tag_name"])
        if data.get("draft") is not False or data.get("prerelease") is not False or version.pre:
            raise UpdateError("仅接受已发布的稳定版本")
        name = f"MaaMiracleNikki-{version}-windows-x64.zip"
        canonical = "MaaMiracleNikki-Windows-x64.zip"
        if any(item.get("name") == canonical for item in data.get("assets", [])):
            name = canonical
        assets = {}
        for item in data.get("assets", []):
            if item.get("name") not in (name, name + ".sha256"):
                continue
            asset_name = item["name"]
            expected = PREFIX + quote(data["tag_name"], safe="") + "/" + asset_name
            if item["browser_download_url"] != expected or not official_asset(expected):
                raise UpdateError("更新资产不属于官方 Release")
            if asset_name in assets or type(item["size"]) is not int or not 0 < item["size"] <= MAX_PACKAGE:
                raise UpdateError("更新资产大小或名称无效")
            digest = item.get("digest") or ""
            if digest and not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
                raise UpdateError("更新资产 SHA256 格式无效")
            assets[asset_name] = Asset(asset_name, expected, item["size"], digest)
        return Release(str(version), str(data.get("body") or "此版本未提供更新说明。")[:100000],
                       assets.get(name), assets.get(name + ".sha256"))
    except (KeyError, TypeError, ValueError) as exc:
        raise UpdateError("GitHub 版本信息无效") from exc


NETWORK_ERRORS = (OSError, URLError, http.client.HTTPException)


class UpdateClient:
    def __init__(self, transport=None, retries=2):
        self.transport = transport or Transport()
        self.retries = retries

    def _attempt(self, operation, cancel):
        for attempt in range(self.retries + 1):
            check_cancel(cancel)
            try:
                return operation()
            except NETWORK_ERRORS as exc:
                if isinstance(exc, HTTPError) and exc.code not in (408, 429, 500, 502, 503, 504):
                    raise UpdateError(f"GitHub 暂不可用（HTTP {exc.code}），请稍后重试") from exc
                if attempt == self.retries:
                    raise UpdateError("连接 GitHub 失败或超时，请检查网络后重试") from exc
                if cancel.wait(0.5 * (attempt + 1)):
                    raise Cancelled("已取消下载")

    def _small(self, url, limit, cancel):
        def read():
            check_cancel(cancel)
            with self.transport.open(url) as response:
                data = response.read(limit + 1)
            check_cancel(cancel)
            if len(data) > limit:
                raise UpdateError("GitHub 响应过大")
            return data
        return self._attempt(read, cancel)

    def latest(self, cancel=None):
        cancel = cancel or Event()
        try:
            return parse_release(json.loads(self._small(API, 2 * 1024**2, cancel)))
        except (ValueError, UnicodeError) as exc:
            raise UpdateError("GitHub 返回的版本信息无法读取") from exc

    def download(self, release, progress=lambda done, total: None, cancel=None, directory=None):
        cancel = cancel or Event()
        if Version(release.version) <= Version(current_version()):
            raise UpdateError("目标版本并非更新版本")
        if not release.package or not release.checksum:
            raise UpdateError("此 Release 缺少 Windows 更新包或 SHA256 文件，请联系开发者")
        package, checksum = release.package, release.checksum
        raw = self._small(checksum.url, 4096, cancel).decode("ascii").strip()
        match = re.fullmatch(r"([0-9a-fA-F]{64})  " + re.escape(package.name), raw)
        if not match:
            raise UpdateError("SHA256 文件格式或文件名不匹配")
        expected = match[1].lower()
        if package.digest and package.digest.lower() != "sha256:" + expected:
            raise UpdateError("GitHub 资产摘要与 SHA256 文件不一致")
        stage = Path(tempfile.mkdtemp(prefix="mmn-download-", dir=directory))
        path = stage / package.name
        def fetch():
            check_cancel(cancel)
            digest, done, started = hashlib.sha256(), 0, time.monotonic()
            progress(0, package.size)
            with self.transport.open(package.url) as response, path.open("wb") as output:
                while True:
                    check_cancel(cancel)
                    if time.monotonic() - started > 1800:
                        raise TimeoutError("download deadline")
                    block = response.read(256 * 1024)
                    if not block:
                        break
                    done += len(block)
                    if done > package.size:
                        raise UpdateError("下载大小超过官方资产声明")
                    output.write(block)
                    digest.update(block)
                    progress(done, package.size)
            check_cancel(cancel)
            if done != package.size:
                raise OSError("下载中断")
            if digest.hexdigest() != expected:
                raise UpdateError("SHA256 校验失败，请重新下载")
        try:
            self._attempt(fetch, cancel)
            from updates.package import inspect_archive
            inspect_archive(path, release.version)
            return path, expected
        except BaseException:
            shutil.rmtree(stage)
            raise
