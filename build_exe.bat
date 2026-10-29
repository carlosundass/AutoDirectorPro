@echo off
REM ============================================================
REM  AutoDirector Pro | Hablemos Entonces Producciones
REM  Genera el programa (.exe) y el instalador.
REM  Doble clic. Necesita Python 3.10 a 3.13 (python.org, con
REM  "Add Python to PATH"). Todos los archivos de esta carpeta
REM  deben estar juntos.
REM ============================================================
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY (
  echo [ERROR] No encontre Python. Instalalo desde https://www.python.org/downloads/
  echo         y marca la casilla "Add Python to PATH" al instalar.
  pause & exit /b 1
)

echo.
echo [1/6] Creando entorno limpio (.venv_build)...
%PY% -m venv .venv_build || ( echo [ERROR] No pude crear el entorno. & pause & exit /b 1 )
call .venv_build\Scripts\activate.bat

echo.
echo [2/6] Instalando dependencias (puede tardar unos minutos)...
python -m pip install --upgrade pip || goto :fail
python -m pip install -r requirements.txt pyinstaller || goto :fail

echo.
echo [3/6] Preparando version, logo e icono...
for /f "delims=" %%v in ('python prepare_release.py') do set "APPVER=%%v"
if not defined APPVER goto :fail
echo      Version del programa: %APPVER%
python make_icon.py || goto :fail

echo.
echo [4/6] Generando AutoDirectorPro.exe (puede tardar 3 a 10 minutos)...
pyinstaller --noconfirm --clean --onefile --windowed --name AutoDirectorPro ^
  --icon icon.ico ^
  --version-file version_info.txt ^
  --add-data "icon.ico;." ^
  --add-data "logo.png;." ^
  --collect-all customtkinter ^
  --collect-all pycapcut ^
  --collect-all pymediainfo ^
  --collect-all imageio_ffmpeg ^
  --collect-all imageio ^
  --collect-all moviepy ^
  --collect-all proglog ^
  --hidden-import audioop ^
  app.py || goto :fail

echo.
echo [5/6] Probando que el .exe trae todo lo necesario...
if exist selftest.txt del selftest.txt
start /wait "" "dist\AutoDirectorPro.exe" --selftest "%~dp0selftest.txt"
if not exist selftest.txt (
  echo [ERROR] El .exe no genero el informe de prueba. Mandame una captura de este mensaje.
  goto :fail
)
type selftest.txt
echo.
findstr /C:"TODO CORRECTO" selftest.txt >nul || goto :fail

echo.
echo [6/6] Creando el instalador...
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC (
  echo.
  echo  El programa ya esta listo en:  %~dp0dist\AutoDirectorPro.exe
  echo.
  echo  Para crear tambien el INSTALADOR necesitas Inno Setup (gratis^):
  echo    1. Abre una terminal y escribe:  winget install -e --id JRSoftware.InnoSetup
  echo       ^(o descargalo de https://jrsoftware.org/isdl.php^)
  echo    2. Vuelve a hacer doble clic en este archivo.
  pause
  exit /b 0
)
"%ISCC%" /DMyVersion=%APPVER% installer.iss || goto :fail
python prepare_release.py --manifest "installer\Instalador-AutoDirectorPro.exe" || goto :fail

echo.
echo ============================================================
echo  LISTO.
echo   Programa:    %~dp0dist\AutoDirectorPro.exe
echo   Instalador:  %~dp0installer\Instalador-AutoDirectorPro.exe
echo   Para actualizar a tus usuarios: sube a GitHub el instalador
echo   y el archivo installer\version.json (mira LEEME_ACTUALIZACIONES.txt).
echo ============================================================
pause
exit /b 0

:fail
echo.
echo [ERROR] Algo fallo. Copia el texto de esta ventana y mandamelo.
pause
exit /b 1
