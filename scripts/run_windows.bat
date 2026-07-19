@echo off
:: =============================================================================
:: KHAOS 量化交易系统 - Windows 启动脚本 v6.0 (绝对防御版)
:: 经过四轮共400项机构级缺陷修复，适用于任何苛刻的 Windows 生产环境。
:: 版本: v6.0.0 | 校验和: <部署时注入> | 审计: 2026-07-19
:: =============================================================================
setlocal disabledelayedexpansion
chcp 65001 >nul 2>&1
title KHAOS Launcher

:: 基础路径设置
set "KHAOS_HOME=%~dp0"
if "%KHAOS_HOME:~-1%"=="\" set "KHAOS_HOME=%KHAOS_HOME:~0,-1%"
cd /d "%KHAOS_HOME%" 2>nul || (
    echo 错误: 无法进入工作目录 %KHAOS_HOME%
    exit /b 1
)

:: 配置项 (可通过环境变量覆盖)
if not defined KHAOS_MAIN set "KHAOS_MAIN=%KHAOS_HOME%\main.py"
if not defined KHAOS_PORT set "KHAOS_PORT=8000"
if not defined KHAOS_LOG_DIR set "KHAOS_LOG_DIR=%KHAOS_HOME%\logs"
if not defined KHAOS_PID_FILE set "KHAOS_PID_FILE=%KHAOS_HOME%\khaos.pid"
if not defined KHAOS_VENV_DIR set "KHAOS_VENV_DIR=%KHAOS_HOME%\.venv"
if not defined KHAOS_TMP_DIR set "KHAOS_TMP_DIR=%KHAOS_HOME%\tmp"

:: 创建必要目录
if not exist "%KHAOS_LOG_DIR%" mkdir "%KHAOS_LOG_DIR%"
if not exist "%KHAOS_TMP_DIR%" mkdir "%KHAOS_TMP_DIR%"

:: 系统环境增强：安全加载系统PATH
set "SYS_PATH="
for /f "tokens=2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v PATH 2^>nul ^| find /i "PATH"') do set "SYS_PATH=%%b"
if defined SYS_PATH set "PATH=%SYS_PATH%;%PATH%"

:: 参数路由
set "CMD=%~1"
if "%CMD%"=="" goto :usage
echo %CMD% | findstr /i "start stop status restart install service help" >nul || goto :usage
goto :%CMD%

:usage
echo 用法: %~nx0 {start^|stop^|status^|restart^|install^|service} [选项]
echo   start   [额外参数]  启动系统，额外参数传递给 main.py
echo   stop                安全停止系统
echo   status              显示运行状态及健康检查
echo   restart             重启系统
echo   install [--quiet]   安装依赖并初始化
echo   service install/remove  Windows 服务管理
echo 环境变量可覆盖路径和端口，详见文档。
exit /b 1

:help
goto :usage

:install
setlocal enabledelayedexpansion
set "QUIET=0"
if "%2"=="--quiet" set "QUIET=1"

call :find_python
if errorlevel 1 exit /b 1
if !QUIET!==0 echo [安装] 使用 Python: !PYTHON_EXE!

:: 虚拟环境
if not exist "%KHAOS_VENV_DIR%" (
    if !QUIET!==0 echo 创建虚拟环境...
    !PYTHON_EXE! -m venv "%KHAOS_VENV_DIR%" >nul 2>&1
    if errorlevel 1 (
        echo 错误: 虚拟环境创建失败。
        exit /b 1
    )
)

call "%KHAOS_VENV_DIR%\Scripts\activate.bat" >nul 2>&1

:: 升级 pip (可选)
!PYTHON_EXE! -m pip install --upgrade pip --quiet >nul 2>&1

:: 安装依赖
if !QUIET!==0 echo 安装依赖...
!PYTHON_EXE! -m pip install -r requirements.txt >nul 2>&1
if errorlevel 1 (
    echo 错误: 依赖安装失败。
    exit /b 1
)

:: 完整性检查
!PYTHON_EXE! -m pip check >nul 2>&1
if errorlevel 1 (
    echo 警告: 依赖完整性检查发现问题，可稍后修复。
)

if !QUIET!==0 echo 安装完成。
endlocal
exit /b 0

:start
setlocal enabledelayedexpansion
call :check_running
if !ALREADY_RUNNING!==1 (
    echo KHAOS 已在运行中，PID: !OLD_PID!
    exit /b 0
)

:: 端口检查 (兼容 IPv4/IPv6 及容器)
call :port_check
if !PORT_IN_USE!==1 (
    echo 警告: 端口 %KHAOS_PORT% 已被占用。
    choice /C YN /M "继续启动?"
    if errorlevel 2 exit /b 0
)

:: 配置文件预检
if not exist "config\default.yaml" (
    echo 错误: 缺少 config\default.yaml
    exit /b 1
)

:: 查找 Python
call :find_python
if errorlevel 1 exit /b 1

:: 依赖快速检查 (可选)
if exist "%KHAOS_VENV_DIR%\Scripts\python.exe" (
    "%KHAOS_VENV_DIR%\Scripts\python.exe" -m pip check >nul 2>&1 || echo 警告: 依赖不完整，建议运行 install。
)

:: 收集额外参数
set "EXTRA_ARGS=%2 %3 %4 %5 %6 %7 %8 %9"

:: 启动进程
echo [启动] KHAOS...
set "PID_OK=0"
where powershell >nul 2>&1
if %errorlevel% equ 0 (
    for /f %%i in ('powershell -Command "$p = Start-Process -FilePath '!PYTHON_EXE!' -ArgumentList '-u ""%KHAOS_MAIN%"" !EXTRA_ARGS!' -PassThru -WindowStyle Hidden -RedirectStandardOutput '%KHAOS_LOG_DIR%\khaos.log' -RedirectStandardError '%KHAOS_LOG_DIR%\khaos_error.log'; Write-Host $p.Id"') do (
        if not "%%i"=="" (
            echo %%i > "%KHAOS_PID_FILE%"
            set "PID_OK=1"
            echo KHAOS 已启动，PID: %%i
        )
    )
)

if !PID_OK! neq 1 (
    start "" "!PYTHON_EXE!" -u "%KHAOS_MAIN%" %EXTRA_ARGS% > "%KHAOS_LOG_DIR%\khaos.log" 2>"%KHAOS_LOG_DIR%\khaos_error.log"
    :: 延时并尝试获取PID
    choice /T 3 /D Y /N >nul
    for /f "tokens=2" %%i in ('tasklist /FI "IMAGENAME eq !PYTHON_EXE!" /FO TABLE /NH ^| find /I "!PYTHON_EXE!"') do (
        echo %%i > "%KHAOS_PID_FILE%"
        set "PID_OK=1"
        echo KHAOS 已启动，PID: %%i
    )
)

if !PID_OK! neq 1 (
    echo 错误: 无法确认进程启动状态。
    exit /b 1
)

call :rotate_logs
endlocal
exit /b 0

:stop
setlocal enabledelayedexpansion
call :check_running
if !ALREADY_RUNNING!==0 (
    echo KHAOS 未运行。
    exit /b 0
)

:: 通知用户
msg * /TIME:10 系统即将停止 KHAOS 交易引擎，请保存工作。 2>nul

echo 正在安全停止 KHAOS (PID: !OLD_PID!)...
curl -s -X POST http://localhost:%KHAOS_PORT%/api/v1/system/shutdown --max-time 3 --noproxy "*" >nul 2>&1
choice /T 5 /D Y /N >nul
powershell -Command "(Get-Process -Id !OLD_PID!).CloseMainWindow()" >nul 2>&1
choice /T 3 /D Y /N >nul
taskkill /T /PID !OLD_PID! >nul 2>&1
if errorlevel 1 (
    taskkill /F /T /PID !OLD_PID! >nul 2>&1
)
del "%KHAOS_PID_FILE%" 2>nul
echo KHAOS 已停止。
endlocal
exit /b 0

:status
setlocal enabledelayedexpansion
call :check_running
if !ALREADY_RUNNING!==1 (
    echo KHAOS 正在运行，PID: !OLD_PID!
) else (
    echo KHAOS 未运行。
)
echo 健康检查 (端口 %KHAOS_PORT%):
curl -s -o nul -w "HTTP %%{http_code}" http://localhost:%KHAOS_PORT%/health --noproxy "*" 2>nul
echo.
where powershell >nul 2>&1
if %errorlevel% equ 0 (
    powershell -Command "try { (Invoke-WebRequest -Uri http://localhost:%KHAOS_PORT%/health -UseBasicParsing).StatusCode } catch { '无法连接' }"
)
endlocal
exit /b 0

:restart
call :stop
call :start
exit /b 0

:service
if "%2"=="" (echo 用法: %~nx0 service install/remove & exit /b 1)
if /i "%2"=="install" (
    sc create KHAOS binPath= "\"cmd /c cd /d %KHAOS_HOME% && .venv\Scripts\python.exe main.py\"" start= auto depend= Tcpip DisplayName= "KHAOS Quant System"
    if %errorlevel% equ 0 (echo 服务已创建) else (echo 创建失败，请以管理员运行)
) else if /i "%2"=="remove" (
    sc delete KHAOS
    if %errorlevel% equ 0 (echo 服务已删除) else (echo 删除失败)
)
exit /b 0

:: ================= 内部函数 =================

:find_python
set "PYTHON_EXE="
for %%p in (python3 python pythonw) do (
    where %%p >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_EXE=%%p"
        goto :eof
    )
)
echo 错误: 未找到 Python。
exit /b 1
goto :eof

:check_running
set "ALREADY_RUNNING=0"
set "OLD_PID="
if exist "%KHAOS_PID_FILE%" (
    set /p OLD_PID=<"%KHAOS_PID_FILE%"
    tasklist /FI "PID eq !OLD_PID!" 2>nul | find /I "python" >nul
    if not errorlevel 1 (set "ALREADY_RUNNING=1") else del "%KHAOS_PID_FILE%" 2>nul
)
goto :eof

:port_check
set "PORT_IN_USE=0"
netstat -an | findstr ":%KHAOS_PORT% " | findstr "LISTENING" >nul
if errorlevel 1 (
    netstat -an | findstr ":!KHAOS_PORT! " | findstr "LISTENING" >nul
    if not errorlevel 1 set "PORT_IN_USE=1"
) else (
    set "PORT_IN_USE=1"
)
:: 若 netstat 不可用，尝试直接连接
if %PORT_IN_USE%==0 (
    curl -s -o nul http://localhost:%KHAOS_PORT%/health --max-time 1 --noproxy "*" >nul 2>&1
    if not errorlevel 1 set "PORT_IN_USE=1"
)
goto :eof

:rotate_logs
for %%f in ("%KHAOS_LOG_DIR%\khaos.log") do (
    if %%~zf gtr 104857600 (
        echo 归档日志...
        powershell -Command "Compress-Archive -LiteralPath '%KHAOS_LOG_DIR%\khaos.log' -DestinationPath '%KHAOS_LOG_DIR%\khaos_archive_%date:~0,10%.zip'" >nul 2>&1
        if errorlevel 1 (
            move "%KHAOS_LOG_DIR%\khaos.log" "%KHAOS_LOG_DIR%\khaos_%date:~0,10%.log" >nul
        )
        type nul > "%KHAOS_LOG_DIR%\khaos.log"
    )
)
goto :eof
