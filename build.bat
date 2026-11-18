@echo off
rem ===========================================================================
rem  OmniAgent Windows - compilation de l'executable (PyInstaller)
rem  Usage : build.bat              (paquet complet + controle de fumee)
rem          build.bat --lite       (paquet allege, sans NumPy)
rem          build.bat --slim       (alias de --lite : meme paquet, produit
rem                                  dans dist\OmniAgent-slim ; Node.js n'est
rem                                  jamais embarque)
rem          build.bat --check      (verifie seulement l'environnement)
rem          build.bat --all        (paquets complet ET allege, puis controle
rem                                  de fumee sur les deux executables)
rem  Les options --lite et --no-smoke peuvent etre combinees.
rem  Aucune variante n'embarque le Node.js du pilote Playwright : les outils
rem  browser_* utilisent le Node.js 18+ du systeme (voir README).
rem  Seul le dossier de la variante demandee est supprime avant compilation
rem  (dist\OmniAgent ou dist\OmniAgent-lite), donc les deux paquets peuvent
rem  coexister. Le paquet complet garde le comportement historique : il
rem  nettoie tout dist\.
rem ===========================================================================
setlocal
cd /d "%~dp0"

set "CHECKONLY=0"
set "LITE=0"
set "SLIM=0"
set "ALL=0"
set "NOSMOKE=0"

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--check" set "CHECKONLY=1"
if /i "%~1"=="--lite" set "LITE=1"
if /i "%~1"=="--slim" set "SLIM=1"
if /i "%~1"=="--all" set "ALL=1"
if /i "%~1"=="--no-smoke" set "NOSMOKE=1"
shift
goto :parse
:parsed

echo [OmniAgent] Verification de l'environnement...
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Python est introuvable dans le PATH.
    goto :fail
)

python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] PyInstaller est absent : pip install -r requirements.txt
    goto :fail
)

python -c "import customtkinter" >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] customtkinter est absent : pip install -r requirements.txt
    goto :fail
)

python app.py --check
if errorlevel 1 echo [AVERTISSEMENT] Le diagnostic signale des points a corriger.

if "%CHECKONLY%"=="1" goto :done

if "%ALL%"=="1" (
    echo [OmniAgent] Construction des paquets complet et allege...
    call :build_variant main
    if errorlevel 1 goto :fail
    call :build_variant lite
    if errorlevel 1 goto :fail
    goto :built
)

if "%SLIM%"=="1" (
    call :build_variant slim
) else if "%LITE%"=="1" (
    call :build_variant lite
) else (
    call :build_variant main
)
if errorlevel 1 goto :fail

:built
echo.
echo [OmniAgent] Termine :
if "%ALL%"=="1" (
    echo             dist\OmniAgent\OmniAgent.exe
    echo             dist\OmniAgent-lite\OmniAgent.exe
) else (
    echo             %EXE%
)
echo             Le dossier qui le contient est distribue en bloc.
goto :done

:fail
endlocal
exit /b 1

:done
endlocal
exit /b 0

rem ---------------------------------------------------------------------------
rem  Sous-programme : compile une variante (main | lite | slim).
rem  Appele uniquement par `call`, jamais atteint par le fil principal.
rem  Code de sortie 1 en cas d'echec (compilation, executable absent, fumee).
rem ---------------------------------------------------------------------------
:build_variant
set "VARIANT=%~1"
set "CLEANALL=0"
if /i "%VARIANT%"=="slim" (
    rem Alias de --lite : meme recette, dossier historique conserve pour les
    rem scripts qui pointent sur dist\OmniAgent-slim.
    set "SPEC=OmniAgent-slim.spec"
    set "OUTDIR=dist\OmniAgent-slim"
    set "LABEL=slim (alias de lite : sans NumPy, sans Node.js embarque)"
) else if /i "%VARIANT%"=="lite" (
    set "SPEC=OmniAgent-lite.spec"
    set "OUTDIR=dist\OmniAgent-lite"
    set "LABEL=allege (sans NumPy)"
) else (
    set "SPEC=OmniAgent.spec"
    set "OUTDIR=dist\OmniAgent"
    set "CLEANALL=1"
    set "LABEL=complet"
)
set "EXE=%OUTDIR%\OmniAgent.exe"

echo [OmniAgent] Nettoyage des sorties precedentes...
if exist build rmdir /s /q build
rem Les variantes --lite et --slim ne touchent qu'a leur propre dossier : le
rem paquet complet de dist\ reste utilisable.
if "%CLEANALL%"=="1" (
    if exist dist rmdir /s /q dist
) else if exist "%OUTDIR%" (
    rmdir /s /q "%OUTDIR%"
)

echo [OmniAgent] Compilation du paquet %LABEL% (cela peut prendre quelques minutes)...
python -m PyInstaller --noconfirm --clean "%SPEC%"
if errorlevel 1 (
    echo [ERREUR] La compilation a echoue.
    exit /b 1
)

if not exist "%EXE%" (
    echo [ERREUR] Executable introuvable apres compilation.
    exit /b 1
)

if not "%NOSMOKE%"=="1" (
    echo [OmniAgent] Controle de fumee : demarrage de l'executable...
    python smoke_exe.py --exe "%EXE%"
    if errorlevel 1 (
        echo [ERREUR] L'executable ne s'ouvre pas ou ne se ferme pas proprement.
        exit /b 1
    )
    echo [OmniAgent] Controle navigateur : Node.js et navigateur du systeme...
    python smoke_exe.py --exe "%EXE%" --browser
    if errorlevel 1 (
        echo [ERREUR] Le navigateur du systeme ne repond pas depuis l'executable.
        exit /b 1
    )
)

echo.
echo [OmniAgent] Poids du paquet produit :
rem Pas de caracteres % ici : cmd les prendrait pour des references de variables.
python -c "import os,sys;r=sys.argv[1];fs=[os.path.join(b,n) for b,_,ns in os.walk(r) for n in ns];print('    ', r, ':', round(sum(os.path.getsize(f) for f in fs)/1048576, 2), 'Mio,', len(fs), 'fichiers')" "%OUTDIR%"
if errorlevel 1 echo     (taille indisponible : mesure Python impossible)
exit /b 0
