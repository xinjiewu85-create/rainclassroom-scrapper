"""Authorized slide images, optional answer annotations, and PDF export."""

import base64
import copy
import json
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from common import DownloadError, atomic_bytes, complete
from option import item_name
from rainclassroom import visible


def validate_image(path):
    try:
        with Image.open(path) as image:
            image.verify()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        raise DownloadError("幻灯片图片损坏或格式无法识别。") from None


def permitted_slides(data, presentation=None):
    slides = data.get("slides", data.get("Slides"))
    if not isinstance(slides, list):
        raise DownloadError("课件缺少幻灯片列表。")
    configurations = []
    for source in (data, data.get("presentation") or {}, presentation or {}):
        conf = source.get("conf")
        if isinstance(conf, str):
            try:
                conf = json.loads(conf)
            except ValueError:
                raise DownloadError("课件访问范围配置无法识别。") from None
        if conf is not None:
            if not isinstance(conf, dict):
                raise DownloadError("课件访问范围配置无法识别。")
            configurations.append(conf)
    result = []
    for slide in slides:
        if not visible(slide):
            continue
        sid = str(slide.get("id", slide.get("Id", "")))
        allowed = True
        for conf in configurations:
            if sid in {str(v) for v in conf.get("hide_slides", [])}:
                allowed = False
            if conf.get("show_presentation") == "film":
                allowed = allowed and sid in {str(v) for v in conf.get("slides", [])}
            elif conf.get("show_presentation") not in (None, "all"):
                raise DownloadError("课件访问模式无法识别，已跳过。")
        if allowed:
            result.append(slide)
    return sorted(result, key=lambda s: int(s.get("index", s.get("Index", s.get("PageIndex", 0)))))


def answer_text(slide):
    problem = slide.get("problem") or {}
    # Only use answers actually returned to this student; never query teacher APIs.
    answer = (problem.get("content") or {}).get("answer")
    if answer is None:
        answer = problem.get("correctAnswer")
    if answer is None:
        return ""
    if isinstance(answer, list):
        answer = "; ".join(str(item) for item in answer)
    return str(answer)


def answer_font(size=24):
    choices = [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in choices:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def annotate(source, target, answer):
    with Image.open(source) as original:
        image = original.convert("RGB")
    try:
        font = answer_font()
        draw = ImageDraw.Draw(image)
        lines, line = [], ""
        for char in "Answer: " + answer:
            if char == "\n" or (line and draw.textlength(line + char, font=font) > image.width - 40):
                lines.append(line)
                line = ""
            if char != "\n":
                line += char
        if line:
            lines.append(line)
        height = 35 * max(1, len(lines)) + 20
        with Image.new("RGB", (image.width, image.height + height), "white") as result:
            result.paste(image, (0, 0))
            ImageDraw.Draw(result).multiline_text(
                (20, image.height + 10), "\n".join(lines), fill="black", font=font, spacing=10
            )
            temporary = target.with_name(target.name + ".part")
            try:
                result.save(temporary, format="PNG")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
    finally:
        image.close()


def images_to_pdf(paths, target):
    if not paths:
        raise DownloadError("课件没有可访问的幻灯片，未生成空 PDF。")
    target = Path(target)
    temporary = target.with_name(target.name + ".part")
    try:
        with ExitStack() as stack:
            images = []
            for path in paths:
                original = stack.enter_context(Image.open(path))
                converted = original.convert("RGB")
                stack.callback(converted.close)
                images.append(converted)
            images[0].save(temporary, "PDF", resolution=100, save_all=True, append_images=images[1:])
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def download_ppt(data, target, files, *, pdf=True, answers=True, presentation=None):
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    slides = permitted_slides(data, presentation)
    images = []
    notes = []
    for position, slide in enumerate(slides, 1):
        cover = slide.get("cover", slide.get("Cover"))
        if not cover:
            raise DownloadError("可访问幻灯片缺少封面地址，未生成不完整 PDF。")
        index = slide.get("index", slide.get("Index", position))
        filename = item_name(str(index), slide.get("id", index))
        path = target / f"{filename}.png"
        files.download(cover, path, validator=validate_image)
        answer = answer_text(slide) if answers else ""
        if answer:
            notes.append(f"第 {index} 页：{answer}")
            annotated = target / f"{filename}-answer.png"
            # Regenerate from the original, so annotations never stack on retry.
            annotate(path, annotated, answer)
            path = annotated
        images.append(path)
    if answers and notes:
        atomic_bytes(target / "题目答案.txt", ("\n".join(notes) + "\n").encode("utf-8"))
    suffix = "-含答案.pdf" if answers else ".pdf"
    output = target.parent / (target.name + suffix)
    if pdf:
        if complete(output):
            print(f"  跳过已有 PDF：{output.name}")
        else:
            images_to_pdf(images, output)
            print(f"  已保存 PDF：{output.name}")
    return output if pdf else target


def print_type2(data, target, host, *, browser="edge", answers=True):
    target = Path(target)
    if complete(target):
        print(f"  跳过已有 PDF：{target.name}")
        return target
    try:
        from selenium import webdriver
        from selenium.common.exceptions import WebDriverException
        from selenium.webdriver.common.print_page_options import PrintOptions
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError:
        raise DownloadError("特殊课件需要 Selenium；请在启动菜单选择“安装特殊课件支持”后重试。") from None
    payload = copy.deepcopy(data)
    payload["Slides"] = permitted_slides(data)
    if not payload["Slides"]:
        raise DownloadError("特殊课件没有可访问的幻灯片。")
    if not answers:

        def remove_answers(value):
            if isinstance(value, dict):
                for key in list(value):
                    if key.casefold() in ("answer", "answers", "correctanswer"):
                        del value[key]
                    else:
                        remove_answers(value[key])
            elif isinstance(value, list):
                for item in value:
                    remove_answers(item)

        remove_answers(payload)
    driver = None
    # A new ephemeral profile is isolated from the student's normal browser.
    try:
        with tempfile.TemporaryDirectory(prefix="rain-print-") as profile:
            options = webdriver.EdgeOptions() if browser == "edge" else webdriver.ChromeOptions()
            options.add_argument("--headless=new")
            options.add_argument(f"--user-data-dir={profile}")
            options.add_argument("--disable-extensions")
            options.add_argument("--no-first-run")
            # Keep the browser sandbox and certificate verification enabled.
            try:
                driver = (
                    webdriver.Edge(options=options)
                    if browser == "edge"
                    else webdriver.Chrome(options=options)
                )
                driver.set_page_load_timeout(60)
                driver.get(f"https://{host}/")
                driver.execute_script(
                    'localStorage.setItem("rain_print", arguments[0]);',
                    json.dumps(payload, ensure_ascii=False),
                )
                driver.get(f"https://{host}/web/print")
                wait = WebDriverWait(driver, 60, poll_frequency=0.5)
                title = str(payload.get("Title") or payload.get("title") or "").strip()
                if not title:
                    raise DownloadError("特殊课件缺少标题，无法核对打印页面。")
                ready_count = 0

                def ready(browser_driver):
                    nonlocal ready_count
                    rendered = browser_driver.execute_script(
                        """
                        return document.readyState === 'complete' &&
                          document.body.innerText.includes(arguments[0]) &&
                          document.images.length >= arguments[1] &&
                          Array.from(document.images).every(i => i.complete && i.naturalWidth > 0) &&
                          (!document.fonts || document.fonts.status === 'loaded');
                    """,
                        title,
                        len(payload["Slides"]),
                    )
                    ready_count = ready_count + 1 if rendered else 0
                    return ready_count >= 3

                wait.until(ready)
                options_print = PrintOptions()
                options_print.background = True
                content = base64.b64decode(driver.print_page(options_print), validate=True)
                if not content.startswith(b"%PDF-"):
                    raise DownloadError("浏览器没有返回有效 PDF。")
                atomic_bytes(target, content)
            finally:
                if driver is not None:
                    driver.quit()
    except DownloadError:
        raise
    except (WebDriverException, OSError, ValueError):
        raise DownloadError("特殊课件打印失败；请检查浏览器及驱动。此打印页适配仍需真实平台验证。") from None
    return target
