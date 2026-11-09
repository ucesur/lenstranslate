@echo off
REM Builds dist\LensTranslate.exe (run this on Windows).
REM Optional: copy your Tesseract-OCR install folder here and rename it "tesseract"
REM (so tesseract\tesseract.exe and tesseract\tessdata exist) to bundle OCR inside the exe.

py -m venv .venv || goto :err
call .venv\Scripts\activate || goto :err
pip install -r requirements.txt pyinstaller || goto :err

set EXTRA=
if exist tesseract set EXTRA=--add-data "tesseract;tesseract"

pyinstaller --noconfirm --onedir --windowed --name LensTranslate %EXTRA% lens.py || goto :err
echo.
echo Done: dist\LensTranslate\LensTranslate.exe
echo Zip the whole dist\LensTranslate folder to share it (keep the files together).
exit /b 0

:err
echo Build failed.
exit /b 1
