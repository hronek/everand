# bookbuilder

Nástroj pro sestavení knihy z HTML do **EPUB** a volitelně do **PDF**, s ohledem na zachování rozložení, obrázků a odkazů.


## Instalace

1) V Python 3.10+ nainstalujte virtuální prostředí pro projekt a závislosti:
```
python -m venv C:\git\everand\.venv
C:\git\everand\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r C:\git\everand\tools\bookbuilder\requirements.txt
```

## Použití

Základní příklad (výchozí řazení podle názvu souboru, interaktivní dotaz na metadata):
```
C:\git\everand\.venv\Scripts\Activate.ps1
python C:\git\everand\tools\bookbuilder\bookbuilder.py \
  --input "C:\git\everand\book" \
  --ask-metadata
```

Řazení podle času vytvoření (ctime):
```
python C:\git\everand\tools\bookbuilder\bookbuilder.py \
  --input "C:\git\everand\book" \
  --output "C:\git\everand\output\book.epub" \
  --title "Il Cammino Neocatecumenale (Vol. I)" \
  --author "Danilo Riccardi" \
  --lang it \
  --sort ctime
```

Předání metadat bez dotazu (neinteraktivně):
```
python C:\git\everand\tools\bookbuilder\bookbuilder.py \
  --input "C:\git\everand\book" \
  --output "C:\git\everand\output\book.epub" \
  --title "Il Cammino Neocatecumenale (Vol. I)" \
  --author "Danilo Riccardi" \
  --lang it
```

### PDF výstup
Pro generování PDF je potřeba nainstalovat/stáhnout portable `wkhtmltopdf` (Windows installer: https://wkhtmltopdf.org/downloads.html). Pokud není v PATH, použijte parametr `--wkhtmltopdf` s plnou cestou k `wkhtmltopdf.exe`.

- Jen PDF (bez EPUB):
```
python C:\git\everand\tools\bookbuilder\bookbuilder.py \
  --input "C:\git\everand\book" \
  --title "Il Cammino Neocatecumenale (Vol. I)" \
  --author "Danilo Riccardi" \
  --lang it \
  --pdf-output "C:\git\everand\output\book.pdf" \
  --wkhtmltopdf "C:\Programy\wkhtmltox\bin\wkhtmltopdf.exe"
```

- EPUB + PDF:
```
python C:\git\everand\tools\bookbuilder\bookbuilder.py \
  --input "C:\git\everand\book" \
  --output "C:\git\everand\output\book.epub" \
  --title "Il Cammino Neocatecumenale (Vol. I)" \
  --author "Danilo Riccardi" \
  --lang it \
  --pdf-output "C:\git\everand\output\book.pdf" \
  --wkhtmltopdf "C:\Programy\wkhtmltox\bin\wkhtmltopdf.exe"
```
## Co je TOC
TOC (Table of Contents) = obsah knihy. V EPUB se z něj generuje navigace (kapitoly v čtečce). V tomto nástroji: každý HTML soubor = kapitola; název kapitoly = první `H1` v souboru.
