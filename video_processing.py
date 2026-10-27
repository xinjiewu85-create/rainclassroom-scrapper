"""Download and join media using stream copy, without a shell or GPU."""

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import certifi

from common import DownloadError, complete, https_url


def best_sources(sources):
    if not isinstance(sources, dict) or not sources:
        return []

    # Existing API uses quality10, quality20, etc. Also accept named qualities.
    def quality(key):
        numbers = re.findall(r"\d+", str(key))
        return (
            int(numbers[-1]) if numbers else {"sd": 1, "hd": 2, "fhd": 3, "uhd": 4}.get(str(key).lower(), 0)
        )

    for key in sorted(sources, key=quality, reverse=True):
        value = sources[key]
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            raise DownloadError("视频地址列表格式无法识别。")
        urls = [https_url(item["url"] if isinstance(item, dict) else item) for item in value]
        if urls:
            return urls
    return []


def find_binary(name, folder=None):
    suffix = ".exe" if os.name == "nt" else ""
    # Only use the explicitly documented tools folder or PATH, not old bundled binaries.
    location = Path(folder) if folder else Path(__file__).resolve().parent / "tools"
    local = location / (name + suffix)
    found = str(local.resolve()) if local.is_file() else shutil.which(name)
    if not found:
        raise DownloadError(
            f"未找到 {name}。请将新版 FFmpeg 的 ffmpeg 和 ffprobe 放入 tools 文件夹或加入 PATH。"
        )
    return found


class VideoDownloader:
    def __init__(self, files, cache, ffmpeg_dir=None):
        self.files = files
        self.cache = Path(cache).resolve()
        self.ffmpeg = find_binary("ffmpeg", ffmpeg_dir)
        self.ffprobe = find_binary("ffprobe", ffmpeg_dir)

    def run(self, args, *, cwd=None, timeout=12 * 60 * 60):
        try:
            result = subprocess.run(
                args,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise DownloadError("FFmpeg 无法运行或处理超时，请检查安装和网络后重试。") from None
        if result.returncode:
            # stderr can contain signed resource URLs. Do not print it.
            raise DownloadError("FFmpeg 处理失败；已保留完整分段，可重试。")
        return result.stdout

    def probe(self, path):
        output = self.run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(Path(path).resolve()),
            ],
            timeout=60,
        )
        try:
            data = json.loads(output)
            streams = [s for s in data["streams"] if s["codec_type"] in ("audio", "video")]
            duration = float(data.get("format", {}).get("duration", 0))
        except (ValueError, KeyError, TypeError):
            raise DownloadError("视频结构无法识别。") from None
        if not streams or duration <= 0:
            raise DownloadError("视频为空或缺少有效的时长。")
        return streams, duration

    def valid(self, path):
        if not complete(path):
            return False
        try:
            self.probe(path)
            return True
        except DownloadError:
            return False

    def convert(self, input_args, target, *, cwd=None, expected_duration=None, tolerance=2):
        target = Path(target)
        temporary = target.with_name(target.stem + ".part" + target.suffix)
        try:
            self.run(
                [
                    self.ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    # Local concat may need harmless AAC priming timestamp corrections.
                    # Remote HLS errors remain fatal to avoid saving incomplete streams.
                    *(["-xerror"] if "concat" not in input_args else []),
                    *input_args,
                    "-map",
                    "0:v?",
                    "-map",
                    "0:a?",
                    "-c",
                    "copy",
                    str(temporary.resolve()),
                ],
                cwd=cwd,
            )
            _, duration = self.probe(temporary)
            if expected_duration is not None and abs(duration - expected_duration) > tolerance:
                raise DownloadError("合并后的时长与分段不一致，已保留分段。")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def download(self, urls, target):
        target = Path(target).resolve()
        urls = [https_url(url) for url in urls]
        if not urls:
            print("  没有可访问的视频。")
            return None
        for extension in (".mp4", ".mkv"):
            candidate = target.with_name(target.name + extension)
            if self.valid(candidate):
                print(f"  跳过已有视频：{candidate.name}")
                return candidate
        target.parent.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(str(target).encode()).hexdigest()[:20]
        work = self.cache / key
        work.mkdir(parents=True, exist_ok=True)
        segments = []
        for i, url in enumerate(urls):
            # Query signatures change; they are neither cached nor logged.
            resource = urlsplit(url)
            identity = hashlib.sha256((resource.netloc + resource.path).encode()).hexdigest()[:10]
            is_hls = resource.path.lower().endswith(".m3u8")
            extension = ".mkv" if is_hls else ".media"
            segment = work / f"{i:04d}-{identity}{extension}"
            if not self.valid(segment):
                print(f"  下载视频分段 {i + 1}/{len(urls)}")
                if is_hls:
                    self.convert(
                        [
                            "-protocol_whitelist",
                            "https,tls,tcp,crypto",
                            "-tls_verify",
                            "1",
                            "-ca_file",
                            certifi.where(),
                            "-rw_timeout",
                            "60000000",
                            "-i",
                            url,
                        ],
                        segment,
                    )
                else:
                    # Keep direct files byte-for-byte; an extra remux can quantize timestamps.
                    self.files.download(url, segment, validator=self.probe)
            segments.append(segment)
        metadata = [self.probe(p) for p in segments]
        fields = (
            "codec_type",
            "codec_name",
            "codec_tag_string",
            "width",
            "height",
            "pix_fmt",
            "sample_rate",
            "channels",
            "channel_layout",
            "time_base",
            "r_frame_rate",
            "profile",
            "level",
            "extradata_size",
        )
        signatures = [[tuple(s.get(k) for k in fields) for s in streams] for streams, _ in metadata]
        if any(sig != signatures[0] for sig in signatures[1:]):
            folder = target.with_name(target.name + "-原始分段")
            folder.mkdir(exist_ok=True)
            for i, segment in enumerate(segments, 1):
                self.convert(["-protocol_whitelist", "file", "-i", str(segment)], folder / f"{i:04d}.mkv")
            raise DownloadError("分段编码、分辨率或帧率不同，无法直接合并；原始分段已保存，可分别播放。")
        # Safe generated basenames, relative to the manifest; no user title interpolation.
        manifest = work / "concat.txt"
        manifest.write_text("".join(f"file '{p.name}'\n" for p in segments), encoding="utf-8")
        input_args = ["-protocol_whitelist", "file", "-f", "concat", "-safe", "1", "-i", str(manifest)]
        expected_duration = sum(duration for _, duration in metadata)
        for extension in (".mp4", ".mkv"):
            candidate = target.with_name(target.name + extension)
            try:
                self.convert(
                    input_args,
                    candidate,
                    cwd=work,
                    expected_duration=expected_duration,
                    tolerance=max(2, len(segments) * 0.2),
                )
                if extension == ".mkv":
                    print("  原始编码不能写入 MP4，已无损保存为 MKV。")
                shutil.rmtree(work)
                print(f"  已保存视频：{candidate.name}")
                return candidate
            except DownloadError:
                if extension == ".mkv":
                    raise
        return None
