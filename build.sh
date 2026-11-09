#!/usr/bin/env bash
# Builds a single-file Linux executable: dist/LensTranslate
# (tesseract itself still comes from apt: sudo apt install tesseract-ocr tesseract-ocr-eng ...)
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --name LensTranslate lens.py
echo
echo "Done: dist/LensTranslate"
