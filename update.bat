@echo off
rem Fetch new results/reports from handball4all and rebuild the HTML site.
cd /d "%~dp0"
python scrape.py %* || goto :error
python build_site.py || goto :error
echo.
echo Fertig. Seite: ..\..\04_Deliverables\Handballstatistiken\index.html
goto :eof
:error
echo Update fehlgeschlagen.
exit /b 1
