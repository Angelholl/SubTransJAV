@echo off
rem =====================================================
rem  SubTransJAV - 卸载脚本
rem  仅清理桌面快捷方式与缓存，不会删除安装文件夹本身
rem =====================================================
setlocal
cd /d "%~dp0"

echo 开始清理桌面快捷方式...
set "LNK1=%USERPROFILE%\Desktop\SubTransJAV.lnk"
set "LNK2=%USERPROFILE%\Desktop\净语翻译.lnk"
set "LNK3=%USERPROFILE%\Desktop\净语翻译 · WhisperJAV Translate.lnk"
set "LNK4=%USERPROFILE%\Desktop\WhisperJAV Translate.lnk"
set "LNK5=%USERPROFILE%\Desktop\wjtranslate-gui.lnk"

if exist "%LNK1%" (
    del /f /q "%LNK1%" 2>nul
    if exist "%LNK1%" (
        echo [WARN] 删除失败，请手动删除: "%LNK1%"
    ) else (
        echo [DONE] 已删除: "%LNK1%"
    )
) else (
    echo [WARN] 本来就不存在，跳过: "%LNK1%"
)

if exist "%LNK2%" (
    del /f /q "%LNK2%" 2>nul
    if exist "%LNK2%" (
        echo [WARN] 删除失败，请手动删除: "%LNK2%"
    ) else (
        echo [DONE] 已删除: "%LNK2%"
    )
) else (
    echo [WARN] 本来就不存在，跳过: "%LNK2%"
)

if exist "%LNK3%" (
    del /f /q "%LNK3%" 2>nul
    if exist "%LNK3%" (
        echo [WARN] 删除失败，请手动删除: "%LNK3%"
    ) else (
        echo [DONE] 已删除: "%LNK3%"
    )
) else (
    echo [WARN] 本来就不存在，跳过: "%LNK3%"
)

if exist "%LNK4%" (
    del /f /q "%LNK4%" 2>nul
    if exist "%LNK4%" (
        echo [WARN] 删除失败，请手动删除: "%LNK4%"
    ) else (
        echo [DONE] 已删除: "%LNK4%"
    )
) else (
    echo [WARN] 本来就不存在，跳过: "%LNK4%"
)

if exist "%LNK5%" (
    del /f /q "%LNK5%" 2>nul
    if exist "%LNK5%" (
        echo [WARN] 删除失败，请手动删除: "%LNK5%"
    ) else (
        echo [DONE] 已删除: "%LNK5%"
    )
) else (
    echo [WARN] 本来就不存在，跳过: "%LNK5%"
)

echo.
echo 开始清理本地缓存...
set "CACHE=%LOCALAPPDATA%\subtransjav\numba_cache"
if exist "%CACHE%" (
    rmdir /s /q "%CACHE%" 2>nul
    if exist "%CACHE%" (
        echo [WARN] 缓存目录删除失败，可能被占用，请稍后手动删除: "%CACHE%"
    ) else (
        echo [DONE] 已删除缓存目录: "%CACHE%"
    )
) else (
    echo [WARN] 缓存目录本来就不存在，跳过: "%CACHE%"
)
rd "%LOCALAPPDATA%\subtransjav" 2>nul

echo.
echo =====================================================
echo  备份提醒：以下数据不会被本脚本删除
echo =====================================================
echo  [1] 翻译成果目录: 文档\SubTransJAV\output
echo      卸载不会删除翻译成果，如需处理请自行操作。
echo  [2] 项目内数据: config\api_keys.bin
echo      服务商密钥库，删除安装文件夹前如需保留请先备份。
echo  [3] 项目内数据: Temp\translation_memory\tm.db
echo      翻译记忆库，删除安装文件夹前如需保留请先备份。

echo.
echo [WARN] 本脚本不删除安装文件夹本身。
echo [WARN] 确认以上备份完成后，请手动删除整个安装文件夹:
echo        "%~dp0"
echo.
echo [DONE] 卸载清理结束。
pause
endlocal
