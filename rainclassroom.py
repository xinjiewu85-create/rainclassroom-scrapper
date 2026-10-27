"""Existing student endpoints, QR login and response normalization."""

import json
import os
import re
import tempfile
import time
from pathlib import Path

import requests

from common import AccessDenied, DownloadError, EndpointUnavailable, check_status


def validate_host(host):
    host = host.lower().strip()
    if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*yuketang\.cn", host):
        raise ValueError("雨课堂域名必须是 yuketang.cn 或其子域名，不含协议和路径。")
    return host


def check_response(payload):
    if not isinstance(payload, dict):
        raise DownloadError("平台返回的数据格式无法识别。")
    markers = []
    for key in ("success", "code", "errcode"):
        if key in payload:
            markers.append(payload[key] is True if key == "success" else str(payload[key]) == "0")
    if not markers:
        raise DownloadError("平台未返回可识别的成功状态，已停止处理这项内容。")
    if not all(markers):
        code = str(payload.get("code", payload.get("errcode", "")))
        message = str(payload.get("msg", payload.get("message", ""))).casefold()
        if code in ("401", "403") or any(
            word in message
            for word in (
                "权限",
                "登录",
                "登陆",
                "未发布",
                "禁止",
                "permission",
                "forbidden",
                "login",
                "unauthorized",
            )
        ):
            raise AccessDenied("登录失效或平台未允许访问这项内容。")
        if code in ("404", "405", "410", "501"):
            raise EndpointUnavailable("该版本接口不可用。")
        raise DownloadError("平台拒绝了请求；未尝试绕过权限。")
    if "data" not in payload:
        raise DownloadError("平台响应缺少 data 字段。")
    return payload["data"]


class RainClient:
    def __init__(self, host="pro.yuketang.cn", retries=2):
        self.host = validate_host(host)
        self.base = f"https://{self.host}"
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": self.base + "/"})
        self.session.cookies.set("xtbz", "ykt", domain=self.host, path="/", secure=True)

    def close(self):
        self.session.cookies.clear()
        self.session.close()

    def api(self, path, *, params=None, headers=None):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("API path must be relative to the configured host")
        for attempt in range(self.retries + 1):
            try:
                with self.session.get(
                    self.base + path, params=params, headers=headers, timeout=(15, 60), allow_redirects=False
                ) as response:
                    if response.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                        time.sleep(min(attempt + 1, 3))
                        continue
                    check_status(response)
                    return check_response(response.json())
            except requests.RequestException:
                if attempt == self.retries:
                    raise DownloadError("无法连接雨课堂，请检查网络后重试。") from None
                time.sleep(min(attempt + 1, 3))
            except ValueError:
                raise DownloadError("平台返回的内容不是有效 JSON。") from None
        raise DownloadError("请求失败。")

    def login(self, timeout=180):
        import qrcode
        import websocket

        # No session file, raw login argument, trace logging or persistent browser profile.
        ws = None
        deadline = time.monotonic() + timeout
        try:
            with tempfile.TemporaryDirectory(prefix="rain-qr-") as folder:
                ws = websocket.create_connection(f"wss://{self.host}/wsapp/", timeout=15, origin=self.base)
                ws.send(
                    json.dumps(
                        {"op": "requestlogin", "role": "web", "version": 1.4, "type": "qrcode", "from": "web"}
                    )
                )
                credentials = None
                print("请用微信扫描二维码，并在手机上确认登录（3 分钟内）。")
                while time.monotonic() < deadline:
                    try:
                        message = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not message:
                        break
                    payload = json.loads(message)
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("UserID") and payload.get("Auth"):
                        credentials = {"UserID": payload["UserID"], "Auth": payload["Auth"]}
                        break
                    if payload.get("qrcode"):
                        qr = qrcode.QRCode(border=2)
                        qr.add_data(payload["qrcode"])
                        qr.make(fit=True)
                        qr.print_ascii(invert=True)
                        if os.name == "nt":
                            qr_path = Path(folder) / "login.png"
                            qr.make_image().save(qr_path)
                            try:
                                os.startfile(str(qr_path))
                            except OSError:
                                print("二维码图片未能打开，请扫描上方字符二维码。")
                if credentials is None:
                    raise DownloadError("扫码登录超时或被取消，请重新启动。")
                with self.session.get(
                    self.base + "/v/course_meta/user_info", timeout=(15, 30), allow_redirects=False
                ) as response:
                    check_status(response)
                with self.session.post(
                    self.base + "/pc/web_login", json=credentials, timeout=(15, 30), allow_redirects=False
                ) as response:
                    check_status(response)
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise DownloadError("登录响应格式无法识别。")
                    if any(k in payload for k in ("success", "code", "errcode")):
                        check_response({**payload, "data": payload.get("data")})
                credentials.clear()
                tokens = [
                    c.value
                    for c in self.session.cookies
                    if c.name == "sessionid" and c.domain.lstrip(".") in (self.host, "yuketang.cn")
                ]
                if not tokens:
                    raise DownloadError("平台没有返回登录 Session，请重新扫码。")
                self.session.cookies.clear()
                self.session.cookies.set("sessionid", tokens[-1], domain=self.host, path="/", secure=True)
                self.session.cookies.set("xtbz", "ykt", domain=self.host, path="/", secure=True)
                print("扫码登录完成。Session 仅保存在本次运行的内存中。")
        except DownloadError:
            raise
        except (requests.RequestException, websocket.WebSocketException, ValueError, OSError):
            raise DownloadError("扫码登录失败，请检查网络后重新启动。") from None
        finally:
            if ws is not None:
                ws.close()

    def courses(self):
        current = self.api("/v2/api/web/courses/list", params={"identity": 2})
        archived = self.api("/v2/api/web/classroom_archive")
        if not isinstance(current, dict) or not isinstance(archived, dict):
            raise DownloadError("课程列表格式无法识别。")
        result, seen = [], set()
        for rows, is_archived in ((current.get("list"), False), (archived.get("classrooms"), True)):
            if not isinstance(rows, list):
                raise DownloadError("课程列表字段缺失。")
            for row in rows:
                identifier = row.get("classroom_id", row.get("id"))
                if identifier is None:
                    raise DownloadError("课程缺少 classroom_id。")
                if str(identifier) in seen:
                    continue
                seen.add(str(identifier))
                result.append(
                    {
                        "id": str(identifier),
                        "name": str(row.get("name") or "未命名班级"),
                        "title": str(
                            (row.get("course") or {}).get("name") or row.get("name") or "未命名课程"
                        ),
                        "teacher": str((row.get("teacher") or {}).get("name") or "未标注教师"),
                        "archived": is_archived,
                    }
                )
        return result

    def lessons(self, course):
        result, seen = [], set()
        for page in range(1000):
            data = self.api(
                f"/v2/api/web/logs/learn/{api_id(course['id'])}",
                params={"actype": -1, "page": page, "offset": 100, "sort": -1},
            )
            if not isinstance(data, dict) or not isinstance(data.get("activities"), list):
                raise DownloadError("课时列表格式无法识别。")
            rows = data["activities"]
            if not rows:
                return result
            added = 0
            for row in rows:
                if row.get("id") is None or row.get("type") is None:
                    raise DownloadError("课时缺少 id 或 type。")
                key = str(row["id"])
                if key not in seen:
                    seen.add(key)
                    result.append({**row, "classroom_id": course["id"]})
                    added += 1
            if not added:
                raise DownloadError("平台重复返回同一页课时，无法确认完整列表。")
            total = data.get("total", data.get("count"))
            if total is not None and len(result) >= int(total):
                return result
            # Fetch until an empty page, even when the server caps page size.
        raise DownloadError("课时页数异常，已停止翻页。")


def api_id(value):
    value = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise DownloadError("平台返回了无效的资源编号。")
    return value


def visible(row):
    return not any(
        row.get(key) in (True, 1, "1", "true")
        for key in ("hidden", "hidden_status", "hiddenStatus", "is_hidden")
    )
