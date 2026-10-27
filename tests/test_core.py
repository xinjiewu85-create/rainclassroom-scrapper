import io
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from PIL import Image

from common import AccessDenied, DownloadError, EndpointUnavailable, FileDownloader, https_url
from main import CourseDownloader, main, mooc_leaves, parser, retry_task
from option import item_name, matches, parse_selection, safe_name
from ppt_processing import download_ppt, permitted_slides, print_type2, validate_image
from rainclassroom import RainClient, check_response, validate_host
from video_processing import VideoDownloader, best_sources


def response(status=200, data=None, body=b"abc", headers=None):
    result = MagicMock()
    result.status_code = status
    result.json.return_value = data
    result.headers = headers or {}
    result.is_redirect = status in (301, 302, 307, 308)
    result.iter_content.return_value = [body]
    result.__enter__.return_value = result
    return result


class SelectionTests(unittest.TestCase):
    def test_select_and_deduplicate(self):
        self.assertEqual(parse_selection("1，3-5,3", 5), [0, 2, 3, 4])
        self.assertEqual(parse_selection("a", 2), [0, 1])

    def test_invalid_selection(self):
        for value in ("0", "-1", "6", "3-1", "1,x", "", "1,", "1-999999999999999"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_selection(value, 5)

    def test_filename_safety(self):
        for value in ("../CON", "A/B\\C", "NUL.txt", "COM1", "LPT¹", "...", "a\x00b", "A: B?. "):
            name = safe_name(value)
            self.assertNotRegex(name, r'[<>:"/\\|?*\x00-\x1f]')
            self.assertFalse(name.endswith((".", " ")))
        self.assertEqual(safe_name("CON"), "_CON")
        self.assertEqual(safe_name("运动生理学-é"), "运动生理学-é")
        self.assertLessEqual(len(safe_name("文" * 200)), 60)
        self.assertNotEqual(item_name("同名课程", 1), item_name("同名课程", 2))

    def test_filter_multiple_names(self):
        self.assertTrue(matches("运动生理学", ["统计", "生理"]))
        self.assertFalse(matches("运动生理学", ["统计", "英语"]))

    def test_cli_has_no_cookie_argument(self):
        options = parser()._option_string_actions
        self.assertNotIn("--session-cookie", options)
        self.assertNotIn("-c", options)
        self.assertIsNone(parser().parse_args([]).content)


class APITests(unittest.TestCase):
    def setUp(self):
        self.client = RainClient(retries=0)
        self.addCleanup(self.client.close)

    def test_host_boundary(self):
        for host in (
            "evil.example",
            "pro.yuketang.cn.evil.org",
            "x@yuketang.cn",
            "https://yuketang.cn",
            "yuketang.cn/path",
        ):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_host(host)
        self.assertEqual(validate_host("PRO.yuketang.cn"), "pro.yuketang.cn")

    def test_fail_closed_without_status(self):
        for payload in ({"data": {}}, [], {"success": False, "code": 0, "data": {}}):
            with self.assertRaises(DownloadError):
                check_response(payload)
        self.assertEqual(check_response({"code": 0, "data": [1]}), [1])

    def test_permission_not_logged_or_fallback(self):
        with self.assertRaises(AccessDenied) as caught:
            check_response({"code": 403, "msg": "secret-cookie", "data": {}})
        self.assertNotIn("secret-cookie", str(caught.exception))
        self.client.session.get = MagicMock(return_value=response(403))
        with self.assertRaises(AccessDenied):
            self.client.api("/test")
        self.assertEqual(self.client.session.get.call_count, 1)

    def test_api_no_redirect_and_finite_timeout(self):
        self.client.session.get = MagicMock(return_value=response(data={"code": 0, "data": []}))
        self.assertEqual(self.client.api("/test"), [])
        kwargs = self.client.session.get.call_args.kwargs
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], (15, 60))

    def test_current_archived_dedup(self):
        self.client.api = MagicMock(
            side_effect=[
                {"list": [{"classroom_id": 1, "name": "班级", "course": {"name": "统计学"}}]},
                {"classrooms": [{"id": 1}, {"id": 2, "name": "往年课程"}]},
            ]
        )
        rows = self.client.courses()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "统计学")
        self.assertTrue(rows[1]["archived"])

    def test_more_than_500_lessons(self):
        pages = [
            {"activities": [{"id": i, "type": 14} for i in range(page * 100, (page + 1) * 100)]}
            for page in range(6)
        ] + [{"activities": []}]
        self.client.api = MagicMock(side_effect=pages)
        self.assertEqual(len(self.client.lessons({"id": 1})), 600)
        self.assertEqual(self.client.api.call_count, 7)

    def test_repeated_page_fails_explicitly(self):
        self.client.api = MagicMock(return_value={"activities": [{"id": 1, "type": 3}]})
        with self.assertRaises(DownloadError):
            self.client.lessons({"id": 1})
        self.assertEqual(self.client.api.call_count, 2)

    def test_login_ignores_intermediate_messages_and_stores_only_memory(self):
        ws = MagicMock()
        ws.recv.side_effect = [
            json.dumps({"subscribe_status": 0}),
            json.dumps({"UserID": "fake-user", "Auth": "fake-auth"}),
        ]
        self.client.session.get = MagicMock(return_value=response())

        def post(*args, **kwargs):
            self.client.session.cookies.set(
                "sessionid", "fake-session", domain="pro.yuketang.cn", secure=True
            )
            return response(data={"success": True})

        self.client.session.post = MagicMock(side_effect=post)
        with patch("websocket.create_connection", return_value=ws), redirect_stdout(io.StringIO()) as output:
            self.client.login()
        self.assertNotIn("fake-session", output.getvalue())
        cookie = next(c for c in self.client.session.cookies if c.name == "sessionid")
        self.assertTrue(cookie.secure)
        self.assertEqual(cookie.domain, "pro.yuketang.cn")
        ws.close.assert_called_once()

    def test_login_disconnect_is_safe(self):
        ws = MagicMock()
        ws.recv.return_value = ""
        with patch("websocket.create_connection", return_value=ws), self.assertRaises(DownloadError):
            self.client.login()
        ws.close.assert_called_once()


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "中文 & $(x)" / "文件.bin"
        self.files = FileDownloader("pro.yuketang.cn", retries=1)
        self.addCleanup(self.files.close)

    def test_atomic_retry_and_existing_skip(self):
        self.files.session.get = MagicMock(
            side_effect=[
                response(body=b"x", headers={"Content-Length": "9"}),
                response(body=b"abc", headers={"Content-Length": "3"}),
            ]
        )
        with patch("common.time.sleep"):
            self.files.download("https://example.invalid/file", self.path)
        self.assertEqual(self.path.read_bytes(), b"abc")
        self.assertFalse(self.path.with_name(self.path.name + ".part").exists())
        self.files.download("https://example.invalid/file", self.path)
        self.assertEqual(self.files.session.get.call_count, 2)

    def test_failure_never_creates_final(self):
        self.files.session.get = MagicMock(return_value=response(body=b"", headers={"Content-Length": "1"}))
        with patch("common.time.sleep"), self.assertRaises(DownloadError):
            self.files.download("https://example.invalid/file", self.path)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.with_name(self.path.name + ".part").exists())

    def test_reject_html_and_permission(self):
        for reply in (response(headers={"Content-Type": "text/html"}), response(403)):
            self.files.session.get = MagicMock(return_value=reply)
            with patch("common.time.sleep"), self.assertRaises(DownloadError):
                self.files.download("https://example.invalid/file", self.path)
            self.assertFalse(self.path.exists())

    def test_reject_insecure_redirect(self):
        self.files.session.get = MagicMock(
            return_value=response(302, headers={"Location": "http://example.invalid/file"})
        )
        with patch("common.time.sleep"), self.assertRaises(DownloadError):
            self.files.download("https://example.invalid/file", self.path)
        for call in self.files.session.get.call_args_list:
            self.assertTrue(call.args[0].startswith("https://"))

    def test_no_login_cookie_on_media_client(self):
        self.assertEqual(list(self.files.session.cookies), [])
        for url in (
            "file:///etc/passwd",
            "http://example.invalid",
            "https://a:b@example.invalid",
            "https://example.invalid/\n",
        ):
            with self.assertRaises(DownloadError):
                https_url(url)

    def test_exception_redaction(self):
        self.files.session.get = MagicMock(
            side_effect=requests.ConnectionError("https://example.invalid/?secret=token")
        )
        with patch("common.time.sleep"), self.assertRaises(DownloadError) as caught:
            self.files.download("https://example.invalid/file", self.path)
        self.assertNotIn("secret", str(caught.exception))


class PPTTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = MagicMock()

        def create_image(url, path, validator=None):
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (200, 120), "white").save(path, format="PNG")
            return path

        self.files.download.side_effect = create_image

    def test_numeric_order_and_permissions(self):
        data = {"slides": [{"id": "a", "index": 10}, {"id": "b", "index": 2}, {"id": "c", "index": 3}]}
        conf = {
            "conf": json.dumps({"show_presentation": "film", "slides": ["a", "b", "c"], "hide_slides": ["c"]})
        }
        self.assertEqual([s["index"] for s in permitted_slides(data, conf)], [2, 10])
        data["slides"][0]["hidden"] = True
        self.assertEqual([s["index"] for s in permitted_slides(data, conf)], [2])

    def test_pdf_and_answer_variants(self):
        data = {
            "slides": [
                {
                    "index": i,
                    "cover": f"https://example.invalid/{i}",
                    "problem": {"content": {"answer": ["A"]}},
                }
                for i in (10, 2)
            ]
        }
        output = download_ppt(data, self.root / "课件", self.files, answers=True)
        self.assertTrue(output.read_bytes().startswith(b"%PDF-"))
        self.assertEqual(len(re.findall(rb"/Type /Page\b", output.read_bytes())), 2)
        self.assertTrue((self.root / "课件" / "题目答案.txt").exists())
        original = download_ppt(data, self.root / "课件", self.files, answers=False)
        self.assertNotEqual(output, original)
        self.assertTrue(original.exists())

    def test_missing_cover_never_writes_partial_pdf(self):
        with self.assertRaises(DownloadError):
            download_ppt(
                {"slides": [{"index": 1, "cover": "https://example.invalid/image"}, {"index": 2}]},
                self.root / "课件",
                self.files,
            )
        self.assertFalse(list(self.root.glob("*.pdf")))

    def test_empty_slides_no_pdf(self):
        with self.assertRaises(DownloadError):
            download_ppt({"slides": []}, self.root / "empty", self.files)
        self.assertFalse(list(self.root.glob("*.pdf")))

    def test_corrupt_image(self):
        path = self.root / "bad.png"
        path.write_text("not image")
        with self.assertRaises(DownloadError):
            validate_image(path)

    def test_type2_missing_dependency_is_actionable(self):
        with (
            patch.dict("sys.modules", {"selenium": None}),
            self.assertRaisesRegex(DownloadError, "安装特殊课件支持"),
        ):
            print_type2({"Slides": [{"PageIndex": 1}]}, self.root / "test.pdf", "pro.yuketang.cn")


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.host = "pro.yuketang.cn"
        self.args = SimpleNamespace(skip_type2=False, no_pdf=False, no_answers=False, browser="edge")
        self.downloader = CourseDownloader(self.client, MagicMock(), MagicMock(), self.args)
        self.lesson = {"id": 7, "courseware_id": 8, "classroom_id": 9, "type": 14, "title": "测试"}

    def test_v1_fallback_returns_without_v3_fallthrough(self):
        self.client.api.side_effect = [EndpointUnavailable(), [{"id": 1}], {"slides": []}]
        with patch.object(self.downloader, "save_ppt") as save:
            self.downloader.ppt(self.lesson, Path("unused"))
        save.assert_called_once()
        self.assertEqual(self.client.api.call_count, 3)

    def test_empty_v1_list(self):
        self.client.api.side_effect = [EndpointUnavailable(), []]
        self.downloader.ppt(self.lesson, Path("unused"))
        self.assertEqual(self.client.api.call_count, 2)

    def test_access_denied_never_uses_fallback(self):
        self.client.api.side_effect = AccessDenied("无权限")
        with self.assertRaises(AccessDenied):
            self.downloader.ppt(self.lesson, Path("unused"))
        self.assertEqual(self.client.api.call_count, 1)

    def test_card_playurls_is_used(self):
        self.client.api.return_value = {
            "Slides": [
                {
                    "PageIndex": 1,
                    "Shapes": [
                        {
                            "ShapeType": 1,
                            "file_title": "Video",
                            "playurls": {"quality10": ["https://example.invalid/1.mp4"]},
                        }
                    ],
                }
            ]
        }
        self.downloader.videos({**self.lesson, "type": 2}, Path("unused"))
        self.downloader.video.download.assert_called_once()

    def test_nonvideo_mooc_leaf(self):
        self.client.api.side_effect = [{"content_info": {"name": "text", "id": 4}}, {"content_info": {}}]
        self.downloader.videos({**self.lesson, "type": 17}, Path("unused"))
        self.downloader.video.download.assert_not_called()

    def test_nested_mooc_leaves(self):
        tree = [{"leaf_list": [{"id": 1}], "section_list": [{"leaf_list": [{"id": 2}]}]}]
        self.assertEqual({x["id"] for x in mooc_leaves(tree)}, {1, 2})

    def test_replay_hidden_segments_and_sort(self):
        self.client.api.return_value = {
            "live": [
                {"url": "https://example.invalid/2", "order": 2},
                {"url": "https://example.invalid/private", "hiddenStatus": 1},
                {"url": "https://example.invalid/1", "order": 1},
            ]
        }
        self.downloader.replay(self.lesson, Path("unused"))
        self.assertEqual(
            self.downloader.video.download.call_args.args[0],
            ["https://example.invalid/1", "https://example.invalid/2"],
        )

    def test_type2_retry_keeps_dispatch(self):
        self.client.api.return_value = {"Slides": [{"PageIndex": 1}]}
        with (
            patch("main.print_type2", side_effect=[DownloadError("temporary"), None]) as printer,
            patch("main.time.sleep"),
        ):
            message = retry_task(lambda: self.downloader.ppt({**self.lesson, "type": 2}, Path("unused")), 1)
        self.assertIsNone(message)
        self.assertEqual(printer.call_count, 2)

    def test_permission_is_not_retried(self):
        action = MagicMock(side_effect=AccessDenied("denied"))
        self.assertEqual(retry_task(action, 3), "denied")
        action.assert_called_once()

    def test_quit_does_not_login(self):
        with patch("builtins.input", return_value="q"), patch("main.RainClient") as client:
            self.assertEqual(main([]), 0)
        client.assert_not_called()


class VideoTests(unittest.TestCase):
    def test_best_quality(self):
        self.assertEqual(
            best_sources(
                {"quality2": ["https://example.invalid/low"], "quality10": ["https://example.invalid/high"]}
            ),
            ["https://example.invalid/high"],
        )
        self.assertEqual(best_sources({}), [])

    def test_shell_disabled_and_errors_redacted(self):
        downloader = VideoDownloader.__new__(VideoDownloader)
        with patch(
            "video_processing.subprocess.run",
            return_value=SimpleNamespace(returncode=1, stdout=b"", stderr=b"token"),
        ) as run:
            with self.assertRaises(DownloadError) as caught:
                downloader.run(["ffmpeg", "-i", "name & $(bad)"])
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.args[0][2], "name & $(bad)")
        self.assertNotIn("token", str(caught.exception))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_real_stream_copy_preserves_frames_resolution_and_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "中文 space & ' quote"
            root.mkdir()
            source = root / "source.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:s=64x48:r=25:d=1",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=1",
                    "-c:v",
                    "mpeg4",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(source),
                ],
                check=True,
                capture_output=True,
            )
            files = MagicMock()

            def download(url, path, validator=None):
                shutil.copy2(source, path)
                return path

            files.download.side_effect = download
            video = VideoDownloader(files, root / "cache")
            result = video.download(
                ["https://example.invalid/a.mp4", "https://example.invalid/b.mp4"], root / "lesson.v1"
            )
            self.assertEqual(result.name, "lesson.v1.mp4")
            streams, duration = video.probe(result)
            original, _ = video.probe(source)
            self.assertAlmostEqual(duration, 2, delta=0.2)
            self.assertEqual(streams[0]["codec_name"], original[0]["codec_name"])
            self.assertEqual(streams[0]["r_frame_rate"], "25/1")
            self.assertEqual((streams[0]["width"], streams[0]["height"]), (64, 48))
            self.assertEqual(streams[1]["codec_name"], original[1]["codec_name"])
            counted = subprocess.run(
                [
                    video.ffprobe,
                    "-v",
                    "error",
                    "-count_frames",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=nb_read_frames",
                    "-of",
                    "csv=p=0",
                    str(result),
                ],
                capture_output=True,
                check=True,
                text=True,
            )
            self.assertEqual(counted.stdout.strip(), "50")
            self.assertEqual(video.download(["https://example.invalid/a.mp4"], root / "lesson.v1"), result)
            self.assertEqual(files.download.call_count, 2)

    def test_hls_arguments_and_failure_atomicity(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("video_processing.find_binary", side_effect=lambda name, _: name),
        ):
            video = VideoDownloader(MagicMock(), folder)
            video.run = MagicMock(side_effect=DownloadError("failed"))
            target = Path(folder) / "segment.mkv"
            with self.assertRaises(DownloadError):
                video.convert(
                    ["-protocol_whitelist", "https,tls,tcp,crypto", "-i", "https://example.invalid/a.m3u8"],
                    target,
                )
            self.assertFalse(target.exists())
            self.assertFalse(list(Path(folder).glob("*.part.*")))
            args = video.run.call_args.args[0]
            self.assertEqual(args[args.index("-c") + 1], "copy")
            self.assertNotIn("-r", args)
            self.assertNotIn("av1_nvenc", args)


class BrowserTests(unittest.TestCase):
    def fake_modules(self, driver, *, fail_wait=False):
        class BrowserFailure(Exception):
            pass

        class Options:
            def __init__(self):
                self.arguments = []

            def add_argument(self, argument):
                self.arguments.append(argument)

        class Wait:
            def __init__(self, browser, timeout, **kwargs):
                self.browser = browser

            def until(self, predicate):
                if fail_wait:
                    raise BrowserFailure("private-token")
                for _ in range(3):
                    result = predicate(self.browser)
                if not result:
                    raise BrowserFailure("not ready")

        webdriver = SimpleNamespace(
            EdgeOptions=Options,
            ChromeOptions=Options,
            Edge=MagicMock(return_value=driver),
            Chrome=MagicMock(return_value=driver),
        )
        modules = {
            "selenium": SimpleNamespace(webdriver=webdriver),
            "selenium.common.exceptions": SimpleNamespace(WebDriverException=BrowserFailure),
            "selenium.webdriver.common.print_page_options": SimpleNamespace(PrintOptions=Options),
            "selenium.webdriver.support.ui": SimpleNamespace(WebDriverWait=Wait),
        }
        return modules, webdriver

    def test_safe_script_arguments_and_sandbox(self):
        import base64

        driver = MagicMock()
        driver.execute_script.return_value = True
        driver.print_page.return_value = base64.b64encode(b"%PDF-1.4\n%%EOF").decode()
        modules, webdriver = self.fake_modules(driver)
        data = {"Title": "Title ${alert(1)} ` quote", "Slides": [{"PageIndex": 1, "answer": ["A"]}]}
        with tempfile.TemporaryDirectory() as root, patch.dict("sys.modules", modules):
            output = print_type2(data, Path(root) / "test.pdf", "pro.yuketang.cn", answers=False)
            self.assertTrue(output.exists())
        script, argument = driver.execute_script.call_args_list[0].args
        self.assertNotIn("${alert", script)
        self.assertIn("arguments[0]", script)
        self.assertNotIn("answer", json.loads(argument)["Slides"][0])
        options = webdriver.Edge.call_args.kwargs["options"]
        self.assertFalse(any("no-sandbox" in a or "ignore-certificate" in a for a in options.arguments))
        profile = next(a.split("=", 1)[1] for a in options.arguments if a.startswith("--user-data-dir="))
        self.assertFalse(Path(profile).exists())
        driver.quit.assert_called_once()

    def test_browser_closed_on_timeout_and_no_empty_pdf(self):
        driver = MagicMock()
        modules, _ = self.fake_modules(driver, fail_wait=True)
        with tempfile.TemporaryDirectory() as root, patch.dict("sys.modules", modules):
            output = Path(root) / "test.pdf"
            with self.assertRaises(DownloadError) as caught:
                print_type2({"Title": "test", "Slides": [{"PageIndex": 1}]}, output, "pro.yuketang.cn")
            self.assertFalse(output.exists())
        self.assertNotIn("private-token", str(caught.exception))
        driver.quit.assert_called_once()


class OfflineFlowTests(unittest.TestCase):
    def test_ppt_flow_requires_no_ffmpeg_or_browser(self):
        client = MagicMock()
        client.host = "pro.yuketang.cn"
        client.courses.return_value = [
            {"id": "1", "title": "生理学", "name": "班1", "teacher": "教师", "archived": False}
        ]
        client.lessons.return_value = [
            {"id": 1, "courseware_id": 2, "type": 14, "title": "第一课", "classroom_id": "1"}
        ]
        client.api.side_effect = [
            {"presentations": [{"id": 3, "title": "课件"}]},
            {"slides": [{"id": 4, "index": 1, "cover": "https://example.invalid/slide"}]},
        ]
        files = MagicMock()

        def download(url, path, validator=None):
            Image.new("RGB", (100, 100), "white").save(path, format="PNG")
            return path

        files.download.side_effect = download
        with (
            tempfile.TemporaryDirectory() as root,
            patch("main.RainClient", return_value=client),
            patch("main.FileDownloader", return_value=files),
            patch("main.VideoDownloader") as video,
        ):
            status = main(["--content", "ppt", "--courses", "1", "--output", root])
            self.assertEqual(status, 0)
            self.assertEqual(len(list(Path(root).rglob("*.pdf"))), 1)
        video.assert_not_called()
        client.close.assert_called_once()
        files.close.assert_called_once()

    def test_windows_launcher_has_crlf_and_no_credentials(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "启动雨课堂下载.bat").read_bytes()
        self.assertIn(b"\r\n", script)
        self.assertNotIn(b"\n", script.replace(b"\r\n", b""))
        self.assertIn(b"setlocal DisableDelayedExpansion", script)
        self.assertNotIn(b"--session-cookie", script)
        self.assertIn(b'pushd "%~dp0"', script)


if __name__ == "__main__":
    unittest.main()
