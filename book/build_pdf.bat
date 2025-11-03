@echo off
setlocal EnableExtensions

rem Vyber Python z virtuálního prostředí, pokud existuje
set "PYTHON=C:\git\everand\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

set "WKHTML=C:\Programy\wkhtmltox\bin\wkhtmltopdf.exe"
if not exist "%WKHTML%" (
  echo [CHYBA] Nenalezeno wkhtmltopdf na "%WKHTML%". Upravte cestu ve skriptu.
  pause
  exit /b 1
)

rem Dotazy na metadata
set /p TITLE=Zadejte TITLE: 
set /p AUTHOR=Zadejte AUTHOR: 
set /p LANG=Zadejte LANG (napr. cs,en,it): 

set "OUT=C:\git\everand\book\%AUTHOR%_%TITLE%_%LANG%.pdf"

echo Python: "%PYTHON%"
"%PYTHON%" --version
"%WKHTML%" --version

echo Spoustim generovani PDF do: "%OUT%"

rem Pokus o kontrolu a pripadnou instalaci zavislosti (ebooklib apod.)
"%PYTHON%" -c "import ebooklib" >NUL 2>&1
if errorlevel 1 (
  echo [INFO] Chybi nektere balicky. Pokusim se nainstalovat requirements...
  "%PYTHON%" -m pip install -r "C:\git\everand\tools\bookbuilder\requirements.txt"
)

set "DUMP_HTML=C:\git\everand\debug\bookbuilder_pdf.html"
"%PYTHON%" C:\git\everand\tools\bookbuilder\bookbuilder.py --input "C:\git\everand\book" --pdf-output "%OUT%" --wkhtmltopdf "%WKHTML%" --dump-pdf-html "%DUMP_HTML%" --title "%TITLE%" --author "%AUTHOR%" --lang "%LANG%"

if errorlevel 1 (
  echo [CHYBA] Generovani PDF selhalo. Zkontrolujte vypis vyse.
  echo [TIP] Zkuste rucne: "%WKHTML%" "%DUMP_HTML%" "C:\git\everand\book\debug.pdf"
) else (
  echo Hotovo.
)

pause
