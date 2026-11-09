[tutorial.webm](https://github.com/user-attachments/assets/4af927a2-90a4-4d51-9e64-5b2633acfb62)

# LensTranslate

A floating magnifier-style window. Put it over any text on screen (browser, PDF,
video subtitles, apps) and it translates the text inside it, in place.
It is a standalone app, not a browser plugin.

## Install (Ubuntu)

```bash
sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-tur \
                 libxcb-cursor0 gnome-screenshot python3-venv
# add more OCR languages as needed: tesseract-ocr-deu, -fra, -spa, -rus, ...

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python lens.py
```

Windows / macOS: install Tesseract (UB Mannheim build / `brew install tesseract`),
then the same `pip install -r requirements.txt` and `python lens.py`.
(macOS asks for Screen Recording permission on first use.)

## Use

| Action | How |
|---|---|
| Move the lens | drag it anywhere |
| Resize | drag edges / bottom corners, or mouse wheel |
| Translate | Space, Enter or double-click |
| Show original again | T |
| Languages, auto mode | right-click |
| Quit | Esc |

"Language of the text under the lens" tells OCR what to read (tick several for
mixed text). "Translate to" is your target language. Settings are remembered.

## Notes

* Translation uses Google Translate (two different endpoints, tried in turn), so it
  needs internet. Google rate-limits by IP address; if you see "too many requests",
  wait a few minutes. Optional offline fallback, used when Google refuses:
  `pip install argostranslate`, then `argospm update && argospm install translate-en_tr`
  (replace `en_tr` with your source/target pair).
* Wayland (default on recent Ubuntu): the app runs through XWayland and captures
  the screen with `gnome-screenshot` (GNOME), so each translation takes about a
  second and the screen may flash. On an Xorg session capture is instant.
  Force the direct Qt grab with `LENS_CAPTURE=qt python lens.py`.
* The translation is painted with the original's size, colour, line spacing and (detected) boldness.
  The exact typeface and italics can't be read from pixels, so it uses your system font;
  pick another one under right-click > "Text font...".
* OCR quality depends on text size: zoom the page a bit for tiny text.

## Build an executable

PyInstaller cannot cross-compile, so build on the OS you want the binary for:

* Windows: double-click `build.bat` -> `dist\LensTranslate.exe`. To bundle OCR, copy your
  `C:\Program Files\Tesseract-OCR` folder next to `build.bat` and rename it `tesseract` first;
  otherwise install Tesseract on the target PC (it is found automatically).
* Ubuntu: `./build.sh` -> `dist/LensTranslate` (still needs `sudo apt install tesseract-ocr ...`).
