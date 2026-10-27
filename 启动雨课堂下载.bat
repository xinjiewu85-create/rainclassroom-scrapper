@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
pushd "%~dp0" || goto failed
set "PYTHONUTF8=1"
if exist ".venv\Scripts\python.exe" goto dependencies
py -3.12 -c "import sys; assert sys.version_info >= (3, 12)" >nul 2>nul
if not errorlevel 1 goto create_py312
py -3 -c "import sys; assert sys.version_info >= (3, 12)" >nul 2>nul
if not errorlevel 1 goto create_py
python -c "import sys; assert sys.version_info >= (3, 12)" >nul 2>nul
if not errorlevel 1 goto create_python
echo 未检测到 Python 3.12 或更高版本。
echo 请从 https://www.python.org/downloads/windows/ 安装 Python 3.12，勾选 Add python.exe to PATH。
goto failed
:create_py312
py -3.12 -m venv .venv
goto created
:create_py
py -3 -m venv .venv
goto created
:create_python
python -m venv .venv
:created
if errorlevel 1 goto failed
:dependencies
".venv\Scripts\python.exe" -c "import sys; assert sys.version_info >= (3,12)" >nul 2>nul
if errorlevel 1 goto old_python
if not exist ".venv\requirements-installed.txt" goto install
fc /b requirements.txt ".venv\requirements-installed.txt" >nul 2>nul
if errorlevel 1 goto install
".venv\Scripts\python.exe" -c "import requests, websocket, qrcode, PIL" >nul 2>nul
if not errorlevel 1 goto menu
:install
echo 首次运行正在安装基础依赖，请保持联网……
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
copy /y requirements.txt ".venv\requirements-installed.txt" >nul
:menu
echo.
echo 1. 开始下载（扫码后选择 PPT 或视频和课程）
echo 2. 安装特殊课件支持（可选，需要 Edge 或 Chrome）
echo 3. 退出
choice /c 123 /n /m "请选择 1、2 或 3："
if errorlevel 3 goto done
if errorlevel 2 goto browser
".venv\Scripts\python.exe" main.py
if errorlevel 1 echo 本次存在未完成内容，请查看上方提示。
pause
goto menu
:browser
".venv\Scripts\python.exe" -m pip install -r requirements-browser.txt
if errorlevel 1 goto failed
echo 安装完成。首次打印可能需要联网获取匹配的浏览器驱动。
goto menu
:old_python
echo .venv 中的 Python 版本过低，请删除项目内 .venv 文件夹后重新启动。
:failed
echo 启动未完成，请查看上方错误。安装失败时请检查网络，然后再次双击。
pause
:done
popd
endlocal
