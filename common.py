"""Bounded HTTPS requests and atomic file writes. No credential logging."""

import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests


class DownloadError(Exception):
    """Messages are safe to display: no raw responses, cookies or signed URLs."""


class AccessDenied(DownloadError):
    pass


class EndpointUnavailable(DownloadError):
    pass


def https_url(url):
    parsed = urlsplit(str(url))
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or any(ord(c) < 32 for c in str(url))
    ):
        raise DownloadError("资源地址不是有效的 HTTPS 地址。")
    return str(url)


def complete(path):
    path = Path(path)
    return path.is_file() and path.stat().st_size > 0


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def check_status(response):
    if response.status_code in (401, 403):
        raise AccessDenied("登录已失效或当前账号没有访问权限；请重新扫码或跳过。")
    if response.status_code in (404, 405, 410, 501):
        raise EndpointUnavailable(f"接口或资源不可用（HTTP {response.status_code}）。")
    if not 200 <= response.status_code < 300:
        raise DownloadError(f"请求失败（HTTP {response.status_code}）。")


class FileDownloader:
    def __init__(self, host, retries=2):
        self.session = requests.Session()
        # Dedicated public-media client: never carries the login cookie.
        self.session.headers.update(
            {"User-Agent": "Mozilla/5.0", "Referer": f"https://{host}/", "Accept-Encoding": "identity"}
        )
        self.retries = retries

    def close(self):
        self.session.close()

    def download(self, url, target, validator=None):
        target = Path(target)
        if complete(target):
            if validator:
                try:
                    validator(target)
                    return target
                except DownloadError:
                    pass
            else:
                return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".part")
        for attempt in range(self.retries + 1):
            try:
                current = https_url(url)
                for _ in range(6):
                    response = self.session.get(current, stream=True, timeout=(15, 60), allow_redirects=False)
                    if response.is_redirect:
                        location = response.headers.get("Location", "")
                        response.close()
                        current = https_url(urljoin(current, location))
                        continue
                    break
                else:
                    raise DownloadError("资源重定向次数过多。")
                with response:
                    check_status(response)
                    media_type = response.headers.get("Content-Type", "").lower()
                    if "text/html" in media_type or "application/json" in media_type:
                        raise DownloadError("资源返回了网页或错误信息，未保存为课件。")
                    size = 0
                    with temporary.open("wb") as output:
                        for chunk in response.iter_content(1024 * 1024):
                            if chunk:
                                output.write(chunk)
                                size += len(chunk)
                    length = response.headers.get("Content-Length")
                    encoded = response.headers.get("Content-Encoding", "identity") != "identity"
                    if not size or (length and not encoded and size != int(length)):
                        raise DownloadError("资源下载不完整。")
                if validator:
                    validator(temporary)
                temporary.replace(target)
                return target
            except AccessDenied:
                raise
            except (requests.RequestException, ValueError, OSError, DownloadError) as exc:
                if attempt == self.retries:
                    if isinstance(exc, DownloadError):
                        raise
                    raise DownloadError("资源下载失败，请检查网络、磁盘空间和保存路径。") from None
                time.sleep(min(attempt + 1, 3))
            finally:
                temporary.unlink(missing_ok=True)
        raise DownloadError("资源下载失败。")
