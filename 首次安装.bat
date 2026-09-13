@echo off
rem =====================================================
rem  SubTransJAV - 首次安装脚本
rem  安装完成后请使用桌面快捷方式启动
rem =====================================================
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"

if exist "%PY%" goto install

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 未找到 Python，请先安装 Python 3.10+ 并加入 PATH
    pause
    exit /b 1
)

echo [SETUP] 创建虚拟环境...
python -m venv .venv
if errorlevel 1 (
    echo [ERROR] venv 创建失败
    pause
    exit /b 1
)

:install
echo [SETUP] 安装依赖...
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -e ".[gui]"
if not errorlevel 1 goto done
echo [SETUP] 默认源安装失败或超时，尝试清华大学镜像源...
"%PY%" -m pip install -e ".[gui]" -i https://pypi.tuna.tsinghua.edu.cn/simple
if not errorlevel 1 goto done
echo [ERROR] 安装失败，请检查网络或查看上方错误信息
pause
exit /b 1

:done
echo.
echo [DONE] 安装完成！请使用桌面"SubTransJAV"快捷方式启动程序。
rem 创建桌面快捷方式（幂等：重复执行覆盖旧快捷方式；失败不中断安装）
if exist "%~dp0create_shortcut.py" (
    "%PY%" "%~dp0create_shortcut.py"
    if errorlevel 1 (
        echo [WARN] 桌面快捷方式创建失败，不影响使用！可手动运行: "%PY%" "%~dp0create_shortcut.py"
    )
)
pause
endlocal
