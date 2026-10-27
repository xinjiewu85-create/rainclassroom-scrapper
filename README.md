# 雨课堂课程下载（rainclassroom-scrapper）

给个人学习使用的命令行工具：**扫码登录 → 选择下载内容和课程 → 按课程保存**。不需要密码、Cookie、服务器、定时任务或 NVIDIA 显卡。

只读取当前账号的学生课程接口及接口返回的可访问资源。**已归档课程指当前账号课程列表中的归档班级**；不代表能下载老师未发布、已隐藏或没有授予你权限的资料。遇到权限拒绝会停止该项，不尝试教师接口或猜测资源编号。

## Windows 第一次使用

1. 从本仓库页面选择 **Code → Download ZIP**，完整解压，例如 `D:\RainClassroom`。不要在压缩包内直接运行；建议用较短的目录路径。
2. 安装 [Python 3.12（64 位）](https://www.python.org/downloads/windows/)，安装时勾选 **Add python.exe to PATH**。代码最低要求 Python 3.12，离线测试使用 3.12。
3. 双击 **`启动雨课堂下载.bat`**。首次运行会自动建立项目内的 `.venv` 并联网安装 4 项基础依赖；以后直接双击同一个文件。
4. 启动菜单选 **1 开始下载**。程序先让你选择：`1` PPT 图片及 PDF、`2` 视频、`3` 两者。然后用微信扫码并在手机上确认。Windows 会尝试打开二维码图片，也会在窗口显示字符二维码。
5. 课程列表会标注 **当前 / 已归档**。输入 `1` 选择一门，输入 `1,3,5-7` 选择多门，输入 `a` 选择列表中全部课程，输入 `q` 退出。

下载视频需要额外完成一次准备：

- 打开 [FFmpeg 官方下载入口](https://ffmpeg.org/download.html#build-windows)，选择其列出的 Windows 构建提供方下载新版压缩包。
- 解压后，把同一包中 `bin` 目录里的 **`ffmpeg.exe`、`ffprobe.exe`** 放到本项目 **`tools`** 文件夹。也可以把该 `bin` 目录加入 Windows PATH。
- 仅下载普通 PPT 时不需要 FFmpeg。旧仓库附带的下载器和 FFmpeg 压缩包已移除，新版不再调用它们。

部分“特殊课件”（原项目 type 2）使用雨课堂自己的网页打印功能：

- 在启动菜单选 **2 安装特殊课件支持**，安装可选 Selenium 依赖。
- Windows 默认使用已安装的 **Edge**；也可用下面的命令切换到 Chrome。
- 首次打印时 Selenium Manager 可能需要联网下载匹配的驱动。驱动获取失败会提示失败，不影响普通 PPT 的依赖安装。
- 特殊课件导出 PDF；`--no-pdf` 或 `--skip-type2` 会跳过此类型。网页打印适配尚未经真实平台验证。

## 保存结果与重试

- 默认保存在项目内 `data`，按“课程名－班级名－编号摘要”建文件夹；其下分 `PPT` 和 `视频`。
- 普通 PPT 下载的是**幻灯片图片**，并按页序合并 PDF，不能恢复成可编辑的原始 `.pptx`。
- 平台确实向当前账号返回题目答案时，会生成标注图片、答案文本和 `-含答案.pdf`。使用 `--no-answers` 生成不添加答案的 PDF。没有返回的答案不会查询或推断。
- 回放、慕课和课件内嵌视频沿用原有类型处理。慕课按 API 的质量编号选择最高档。
- 视频使用 FFmpeg `-c copy`，不重新编码、不设置目标帧率；优先 MP4，容器不兼容时尝试 MKV。如果分段编码、分辨率等不同，保留可分别播放的原始分段并报告无法直接合并。
- 下载和合并先写临时文件，成功后才替换最终文件。图片会校验格式，视频会检查音视频流和时长。已有完整文件自动跳过。
- 默认失败后额外重试 2 次。权限拒绝不反复重试；失败项目写入 `data\下载失败.txt`。重新启动、扫码并选择相同课程即可重试。成功的图片和完整视频分段会复用，**未完成的单个文件重新下载，不做字节级断点续传**。
- 视频分段临时放在输出目录下的 `.video-cache`，成功合并后清理。请预留源文件与合并文件同时存在的磁盘空间。
- 文件名含短编号摘要，防止同名课程或课件互相覆盖。尽量保留较短的项目和输出路径，以免 Windows 或外部程序遇到路径长度限制。

## 登录与安全

- 每次运行都扫码。Session 仅在内存中使用，退出时清除，不写入项目、日志或配置；已移除明文 Cookie 命令行参数和调试器 Cookie 输入。
- 新版不会读取旧版的 `data\session.txt`。如果以前运行过旧版，请删除该文件；曾向别人公开过登录信息，应通过平台退出旧会话后重新扫码。
- 登录和 API 请求只发往指定的 `yuketang.cn` 域名或其子域名，使用 HTTPS；API 不跟随重定向。下载资源使用独立客户端，不携带登录 Cookie。
- 不打印完整 API 响应、原始异常或带签名的下载地址。FFmpeg 子进程不接收 Session；HLS 的临时资源 URL 仍会作为 FFmpeg 参数存在于本机进程信息中，请勿分享完整进程命令行。
- 外部程序以参数列表启动，`shell=False`，不再拼接 PowerShell/Shell 命令。
- Selenium 按需导入，使用临时独立浏览器配置，不关闭沙箱、不忽略证书校验；通过 `arguments[0]` 传递课件 JSON，避免 JavaScript 字符串插值；结束后关闭浏览器并清理配置。
- 示例中的账号标识和签名链接已从当前版本删除。历史 Git 提交保留，不声称抹除了历史记录。

## 可选命令行用法

通常双击启动文件即可。以下命令在项目文件夹的 PowerShell 执行（首次双击后已有 `.venv`）：

```powershell
# 交互选择课程，只下载 PPT 图片和 PDF
.\.venv\Scripts\python.exe main.py --content ppt

# 同时下载 PPT 和视频，选择列表中的第 1、3～5 门课
.\.venv\Scripts\python.exe main.py --content all --courses "1,3-5"

# 按课程名称筛选；两个课程关键词之间为“或”
.\.venv\Scripts\python.exe main.py --content ppt --course-name-filter "运动生理" --course-name-filter "统计"

# 按课时名称筛选，设置保存目录
.\.venv\Scripts\python.exe main.py --content video --lesson-name-filter "第一章" --output "D:\课程资料"

# 特殊课件改用 Chrome
.\.venv\Scripts\python.exe main.py --content ppt --browser chrome

# 查看完整参数
.\.venv\Scripts\python.exe main.py --help
```

| 参数 | 含义 |
| --- | --- |
| `--content ppt/video/all` | 下载内容，省略时交互选择 |
| `--courses "1,3-5"` 或 `--courses a` | 从当前显示的列表选择课程，序号从 1 开始 |
| `-y` / `--ykt-host` | 雨课堂域名，默认 `pro.yuketang.cn`；不同入口登录和课程归属可能不同 |
| `-cnf` / `--course-name-filter` | 筛选课程名或班级名，可重复 |
| `-lnf` / `--lesson-name-filter` | 筛选课时标题，可重复 |
| `--output` | 输出目录，默认项目内 `data` |
| `--retries 0..5` | 额外重试次数，默认 2 |
| `--no-pdf` | 普通课件仅保存图片；跳过特殊课件 |
| `--no-answers` | 不添加答案标注 |
| `--skip-type2` | 跳过浏览器打印的特殊课件 |
| `--browser edge/chrome` | 特殊课件浏览器；Windows 默认 Edge |
| `--ffmpeg-dir` | 指定包含 FFmpeg、ffprobe 的文件夹 |

旧 README 中的 `main_windows.py` 不存在。新版入口是 `main.py`；旧的 IDM、aria2、Cookie、`-ds/-nv` 等参数已删除，请使用上表。

## 开发检查（无需真实账号）

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m compileall -q main.py common.py rainclassroom.py option.py ppt_processing.py video_processing.py tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用虚构 API、测试图片、本地合成视频，不连接雨课堂。真实视频合并测试需要 PATH 中有 `ffmpeg` 和 `ffprobe`；缺少时显示 skipped，不伪报为通过。

**验证边界：**已检查的是代码与离线行为。真实扫码协议、当前/归档课程 API 的现行返回结构、实际 PPT/回放/慕课下载、签名链接有效期及特殊课件打印页面仍需在你本机使用自己的账号验证。本次执行环境为 Linux，Windows 批处理和 Edge/Chrome 驱动流程只做静态及模拟检查，未在 Windows 上实跑。服务返回不明确的错误时停止，不将权限错误当成旧版接口进行回退；只有明确的接口不存在/不支持状态才尝试原项目旧接口。

实现依据：[FFmpeg streamcopy](https://ffmpeg.org/ffmpeg.html#Streamcopy)、[FFmpeg concat demuxer](https://ffmpeg.org/ffmpeg-formats.html#concat)、[Selenium Manager](https://www.selenium.dev/documentation/selenium_manager/)、[Selenium Print Page](https://www.selenium.dev/documentation/webdriver/interactions/print_page/)。雨课堂接口来自本项目原代码，并非官方稳定 API 承诺。
