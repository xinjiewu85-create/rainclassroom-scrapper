"""Interactive rainclassroom downloader. Importing this module has no side effects."""

import argparse
import sys
import time
from pathlib import Path

from common import AccessDenied, DownloadError, EndpointUnavailable, FileDownloader
from option import choose_courses, item_name, matches, safe_name
from ppt_processing import download_ppt, print_type2
from rainclassroom import RainClient, api_id, visible
from video_processing import VideoDownloader, best_sources

ROOT = Path(__file__).resolve().parent


def parser():
    result = argparse.ArgumentParser(description="雨课堂个人课程下载：扫码 → 选课程 → 下载。")
    result.add_argument(
        "-y", "--ykt-host", default="pro.yuketang.cn", help="雨课堂域名，默认 pro.yuketang.cn"
    )
    result.add_argument("--content", choices=("ppt", "video", "all"), help="下载内容；省略时交互选择")
    result.add_argument("--courses", help="课程列表序号，如 1,3-5 或 a；省略时交互选择")
    result.add_argument("-cnf", "--course-name-filter", action="append", help="课程/班级名称关键词，可重复")
    result.add_argument("-lnf", "--lesson-name-filter", action="append", help="课时名称关键词，可重复")
    result.add_argument("--output", type=Path, default=ROOT / "data", help="保存目录，默认项目内 data")
    result.add_argument(
        "--retries",
        type=int,
        choices=range(0, 6),
        default=2,
        metavar="0..5",
        help="失败后的额外重试次数，默认 2",
    )
    result.add_argument("--no-pdf", action="store_true", help="普通 PPT 仅下载图片，不合并 PDF；跳过特殊课件")
    result.add_argument("--no-answers", action="store_true", help="不添加平台已返回的题目答案")
    result.add_argument("--skip-type2", action="store_true", help="跳过需要浏览器打印的特殊课件")
    result.add_argument(
        "--browser",
        choices=("edge", "chrome"),
        default="edge" if sys.platform == "win32" else "chrome",
        help="特殊课件使用的浏览器（Windows 默认 Edge）",
    )
    result.add_argument("--ffmpeg-dir", type=Path, help="包含 ffmpeg 与 ffprobe 的目录")
    return result


def content_choice():
    while True:
        print("下载内容：1 = PPT 图片和 PDF；2 = 视频；3 = PPT 和视频；q = 退出")
        value = input("请输入：").strip().lower()
        if value == "q":
            return None
        if value in ("1", "2", "3"):
            return {"1": "ppt", "2": "video", "3": "all"}[value]
        print("请输入 1、2、3 或 q。")


def mooc_leaves(content):
    if isinstance(content, list):
        for node in content:
            yield from mooc_leaves(node)
    elif isinstance(content, dict):
        if not visible(content):
            return
        children = [content[k] for k in ("chapter_list", "section_list", "leaf_list") if content.get(k)]
        if children:
            for child in children:
                yield from mooc_leaves(child)
        elif content.get("id") is not None and visible(content):
            yield content


class CourseDownloader:
    def __init__(self, client, files, video, args):
        self.client, self.files, self.video, self.args = client, files, video, args

    def cards(self, lesson):
        return self.client.api(
            f"/v2/api/web/cards/detlist/{api_id(lesson['courseware_id'])}",
            params={"classroom_id": lesson["classroom_id"]},
        )

    def ppt(self, lesson, folder):
        name = item_name(lesson.get("title") or "课件", lesson["id"])
        kind = int(lesson["type"])
        if kind == 2:
            if self.args.skip_type2 or self.args.no_pdf:
                print("  按设置跳过特殊课件。")
                return
            data = self.cards(lesson)
            suffix = "-含答案.pdf" if not self.args.no_answers else ".pdf"
            print_type2(
                data,
                folder / (name + suffix),
                self.client.host,
                browser=self.args.browser,
                answers=not self.args.no_answers,
            )
            return
        if kind not in (3, 14):
            return
        try:
            summary = self.client.api(
                "/api/v3/lesson-summary/student", params={"lesson_id": lesson["courseware_id"]}
            )
        except EndpointUnavailable:
            presentations = self.client.api(
                f"/v2/api/web/lessonafter/{api_id(lesson['courseware_id'])}/presentation",
                params={"classroom_id": lesson["classroom_id"]},
            )
            if not isinstance(presentations, list):
                raise DownloadError("旧版课件列表格式无法识别。") from None
            for ppt in presentations:
                if not visible(ppt):
                    continue
                data = self.client.api(
                    f"/v2/api/web/lessonafter/presentation/{api_id(ppt['id'])}",
                    params={"classroom_id": lesson["classroom_id"]},
                )
                self.save_ppt(data, ppt, lesson, folder)
            if not presentations:
                print("  没有可访问的 PPT。")
            return  # Never fall through into the v3 branch after a v1 fallback.
        presentations = summary.get("presentations")
        if not isinstance(presentations, list):
            raise DownloadError("课件列表格式无法识别。")
        for ppt in presentations:
            if not visible(ppt):
                continue
            data = self.client.api(
                "/api/v3/lesson-summary/student/presentation",
                params={"presentation_id": ppt["id"], "lesson_id": lesson["courseware_id"]},
            )
            self.save_ppt(data, ppt, lesson, folder)
        if not presentations:
            print("  没有可访问的 PPT。")

    def save_ppt(self, data, ppt, lesson, folder):
        title = (
            ppt.get("title")
            or data.get("title")
            or (data.get("presentation") or {}).get("title")
            or lesson.get("title")
            or "课件"
        )
        name = item_name(title, f"{lesson['id']}-{ppt['id']}")
        download_ppt(
            data,
            folder / name,
            self.files,
            pdf=not self.args.no_pdf,
            answers=not self.args.no_answers,
            presentation=ppt,
        )

    def videos(self, lesson, folder):
        kind = int(lesson["type"])
        if kind in (3, 14):
            self.replay(lesson, folder)
        elif kind in (15, 17):
            self.mooc(lesson, folder)
        elif kind == 2:
            self.card_videos(lesson, folder)

    def replay(self, lesson, folder):
        try:
            data = self.client.api(
                "/api/v3/lesson-summary/replay", params={"lesson_id": lesson["courseware_id"]}
            )
            segments = data.get("live", [])
        except EndpointUnavailable:
            data = self.client.api(
                "/v/lesson/get_lesson_replay_timeline/", params={"lesson_id": lesson["courseware_id"]}
            )
            segments = data.get("live_timeline", [])
        if not visible(data):
            print("  平台标记回放不可见，已跳过。")
            return
        if not isinstance(segments, list):
            raise DownloadError("回放分段格式无法识别。")
        segments = [s for s in segments if visible(s)]
        segments.sort(key=lambda s: int(s.get("order", 0)))
        urls = [s.get("url") or s.get("replay_url") for s in segments]
        if any(not u for u in urls):
            raise DownloadError("回放分段缺少地址，未生成不完整视频。")
        self.video.download(urls, folder / item_name(lesson.get("title") or "回放", lesson["id"]))

    def mooc(self, lesson, folder):
        headers = {"Xtbz": "ykt", "Classroom-Id": str(lesson["classroom_id"])}
        data = self.client.api(
            f"/c27/online_courseware/xty/kls/pub_news/{api_id(lesson['courseware_id'])}/", headers=headers
        )
        content = data.get("content_info")
        if not content:
            print("  没有可访问的慕课内容。")
            return
        seen = set()
        for leaf in mooc_leaves(content):
            identifier = str(leaf["id"])
            if identifier in seen:
                continue
            seen.add(identifier)
            detail = self.client.api(
                f"/mooc-api/v1/lms/learn/leaf_info/{api_id(lesson['classroom_id'])}/{api_id(identifier)}/",
                headers=headers,
            )
            if not visible(detail):
                continue
            info = detail.get("content_info") or {}
            media = info.get("media") or {}
            if not isinstance(media, dict) or not media.get("ccid"):
                continue  # Text/exercises have no video; do not guess a media ID.
            play = self.client.api(
                "/api/open/audiovideo/playurl",
                params={"video_id": media["ccid"], "provider": "cc", "is_single": 0, "format": "json"},
            )
            urls = best_sources((play.get("playurl") or {}).get("sources"))
            name = item_name(
                leaf.get("title") or leaf.get("name") or "慕课视频", f"{lesson['id']}-{identifier}"
            )
            self.video.download(urls, folder / name)

    def card_videos(self, lesson, folder):
        from ppt_processing import permitted_slides

        data = self.cards(lesson)
        for slide in permitted_slides(data):
            for index, shape in enumerate(slide.get("Shapes", [])):
                if shape.get("ShapeType") != 1 or not visible(shape) or not shape.get("playurls"):
                    continue
                urls = best_sources(shape["playurls"])
                name = item_name(
                    shape.get("file_title") or "课件视频", f"{lesson['id']}-{slide.get('PageIndex')}-{index}"
                )
                self.video.download(urls, folder / name)


def retry_task(action, retries):
    for attempt in range(retries + 1):
        try:
            action()
            return None
        except AccessDenied as exc:
            return str(exc)
        except DownloadError as exc:
            message = str(exc)
        except (KeyError, TypeError, ValueError, AttributeError, IndexError):
            message = "平台数据结构与预期不一致，此项未完成。"
        except OSError:
            message = "无法读写文件，请检查磁盘空间、文件占用和路径长度。"
        if attempt < retries:
            print(f"  失败，正在重试 {attempt + 1}/{retries}。")
            time.sleep(min(attempt + 1, 3))
    return message


def run(args):
    args.output = args.output.expanduser().resolve()
    if args.content is None:
        args.content = content_choice()
    if args.content is None:
        return 0
    # No environment/network changes before the user chooses what to download.
    client = RainClient(args.ykt_host, args.retries)
    files = FileDownloader(client.host, args.retries)
    try:
        video = (
            VideoDownloader(files, args.output / ".video-cache", args.ffmpeg_dir)
            if args.content in ("video", "all")
            else None
        )
        if (ROOT / "data/session.txt").exists():
            print("提示：旧版 data/session.txt 含明文登录信息，请删除；新版不会读取它。")
        client.login()
        courses = [
            c for c in client.courses() if matches(f"{c['title']} {c['name']}", args.course_name_filter)
        ]
        if not courses:
            print("当前账号没有符合筛选条件的课程。")
            return 0
        selected = choose_courses(courses, args.courses)
        if not selected:
            return 0
        args.output.mkdir(parents=True, exist_ok=True)
        downloader = CourseDownloader(client, files, video, args)
        failed = []
        for course in selected:
            print(f"\n课程：{safe_name(course['title'])}")
            folder = args.output / item_name(f"{course['title']}-{course['name']}", course["id"])
            lessons = []
            message = retry_task(
                lambda rows=lessons, selected_course=course: rows.extend(client.lessons(selected_course)),
                args.retries,
            )
            if message:
                failed.append(f"{course['title']}：课程列表：{message}")
                print(message)
                continue
            for lesson in lessons:
                if not visible(lesson) or not matches(lesson.get("title", ""), args.lesson_name_filter):
                    continue
                kind = int(lesson["type"])
                actions = []
                if args.content in ("ppt", "all") and kind in (2, 3, 14):
                    actions.append(
                        (
                            "PPT",
                            lambda row=lesson, destination=folder: downloader.ppt(row, destination / "PPT"),
                        )
                    )
                if video and kind in (2, 3, 14, 15, 17):
                    actions.append(
                        (
                            "视频",
                            lambda row=lesson, destination=folder: downloader.videos(
                                row, destination / "视频"
                            ),
                        )
                    )
                for label, action in actions:
                    title = safe_name(lesson.get("title") or "未命名课时")
                    print(f"  {label}：{title}")
                    message = retry_task(action, args.retries)
                    if message:
                        print(f"  未完成：{message}")
                        failed.append(f"{safe_name(course['title'])} / {title} / {label}：{message}")
        report = args.output / "下载失败.txt"
        if failed:
            report.write_text("\n".join(failed) + "\n", encoding="utf-8")
            print(
                f"\n有 {len(failed)} 项未完成，详见 {report}。重新启动并选择相同课程可重试，完整文件会跳过。"
            )
            return 1
        if report.exists():
            report.write_text("本次所选内容没有下载失败项。\n", encoding="utf-8")
        print(f"\n本次所选内容处理完成。保存位置：{args.output}")
        return 0
    finally:
        files.close()
        client.close()


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return run(args)
    except (EOFError, KeyboardInterrupt):
        print("\n已取消；完整下载的文件会保留。")
        return 130
    except DownloadError as exc:
        print(f"未完成：{exc}", file=sys.stderr)
        return 1
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        print("输入或平台响应格式无法识别，已停止。", file=sys.stderr)
        return 1
    except OSError:
        print("文件操作失败，请检查磁盘空间和路径长度。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
