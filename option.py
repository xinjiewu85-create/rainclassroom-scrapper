"""CLI choices and portable path components (never whole paths)."""

import hashlib
import re


def safe_name(value, limit=60):
    original = str(value)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", original).strip(" .")
    if not name:
        name = "未命名"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", name, re.I):
        name = "_" + name
    if len(name) > limit:
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:8]
        name = name[: limit - 9].rstrip(" .") + "-" + digest
    return name


def item_name(title, identifier):
    # Stable IDs avoid collisions and changing numbering when new lessons appear.
    digest = hashlib.sha256(str(identifier).encode("utf-8")).hexdigest()[:10]
    return f"{safe_name(title, 45)}-{digest}"


def matches(value, filters):
    return not filters or any(f.casefold() in str(value).casefold() for f in filters)


def parse_selection(text, count):
    text = text.strip().replace("，", ",")
    if text.lower() in ("a", "all", "全部"):
        return list(range(count))
    selected = set()
    for part in text.split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", part)
        if not match:
            raise ValueError("请输入课程序号，例如 1,3-5；输入 a 选择全部。")
        start = int(match[1])
        end = int(match[2] or start)
        if not 1 <= start <= end <= count:
            raise ValueError("序号超出范围，或区间顺序错误。")
        selected.update(range(start - 1, end))
    return sorted(selected)


def choose_courses(courses, selection=None):
    for index, course in enumerate(courses, 1):
        state = "已归档" if course.get("archived") else "当前"
        print(f"{index}. [{state}] {course['title']} ({course['name']}) — {course['teacher']}")
    if selection is not None:
        return [courses[i] for i in parse_selection(selection, len(courses))]
    while True:
        value = input("选择课程（如 1,3-5；a 全部；q 退出）：").strip()
        if value.lower() == "q":
            return []
        try:
            return [courses[i] for i in parse_selection(value, len(courses))]
        except ValueError as exc:
            print(exc)
