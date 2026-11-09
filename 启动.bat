@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYEXE="
where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PYEXE=python"
if not defined PYEXE (
    where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul && set "PYEXE=py -3"
)
if not defined PYEXE goto nopython

%PYEXE% -c "import PySide6.QtCharts" >nul 2>nul
if errorlevel 1 (
    echo.
    echo 首次运行需要安装界面库 PySide6（约 100 MB），正在安装…
    echo.
    %PYEXE% -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo.
        echo 安装失败。请检查网络，或手动执行：%PYEXE% -m pip install -r requirements.txt
        echo 国内网络可以加镜像：-i https://pypi.tuna.tsinghua.edu.cn/simple
        pause
        exit /b 1
    )
)

set "PYW="
for /f "usebackq delims=" %%i in (`%PYEXE% -c "import os, sys; p = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'); print(p if os.path.exists(p) else sys.executable)"`) do set "PYW=%%i"
if not defined PYW set "PYW=python"
start "" "%PYW%" "%~dp0app.py" %*
exit /b 0

:nopython
echo.
echo 没有找到 Python 3.10 或更新版本。请先安装 Python（安装时勾选 Add to PATH）。
echo 下载地址：https://www.python.org/downloads/windows/
echo.
pause
exit /b 1
