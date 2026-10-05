# darkpdf

https://clayikari.pythonanywhere.com/

Turn a white-background / black-text PDF into a black-background / white-text
one. Single file, no system tools, works on Windows/macOS/Linux.

```bash
pip install -r requirements.txt
python darkpdf.py report.pdf            # -> report_dark.pdf
python darkpdf.py report.pdf -o out.pdf
python darkpdf.py papers/ -o converted/ # whole folder
```

## The two modes

### `--mode vector` (default)

Pages stay real vector content. Text is still selectable, searchable and
copyable, links and annotations still work, and the file stays about the same
size. Nothing is rasterised, so it is fast even on a 500-page document.

It works by rewriting colours rather than pixels:

| what | how |
| --- | --- |
| text, vector art | every colour operator (`rg` `RG` `g` `G` `k` `K` `sc` `scn` …) has each component replaced by `1 - c`, with `q`/`Q` graphics-state tracking so nested blocks stay correct |
| form XObjects, tiling patterns, Type 3 glyphs | their content streams are rewritten too, recursively |
| embedded images | 8-bit gray/RGB samples are inverted byte by byte; indexed images have their palette inverted; JPEGs are decoded, inverted and re-encoded (including JPEGs hidden behind a `[/FlateDecode /DCTDecode]` chain) |
| gradients | the colour endpoints of exponential/stitching/sampled PDF functions are inverted |
| page background | an opaque black rectangle is painted underneath, so pages that never draw a background get one |

CMYK is converted to RGB, inverted, and converted back, so complements come out
right (red becomes cyan, not an out-of-gamut mess). Anything that cannot be
modelled safely is left untouched rather than corrupted - soft masks, blend
modes, `Separation`/`Lab` colour spaces, 16-bit and sub-byte images, JPEG2000
and CCITT scans - and it stays visible against the black page. If a document
needs those handled too, use `--mode raster`.

### `--mode raster`

Each page is rendered to a bitmap, the pixels are inverted, and the page is
rebuilt from that image. Slower and much larger, but pixel-exact for anything
the recolourer cannot model. `--keep-text` (on by default) adds an invisible
text layer so the result is still searchable and selectable; `--no-keep-text`
bakes the text into the pixels for the smallest, simplest file.

Use raster mode when a document comes out looking wrong in vector mode - scans
with blend modes, colour-managed `Separation` colours, unusual effects.

## Options

```
-o, --output PATH      output PDF (single input) or output directory (batch)
-m, --mode MODE        vector | raster                     (default: vector)
-d, --dpi N            render resolution, raster mode      (default: 200)
-f, --format FORMAT    png | jpeg, raster mode             (default: png)
-q, --quality N        JPEG quality 1-100                  (default: 90)
    --suffix TEXT      suffix for generated names          (default: _dark)
    --no-keep-text     raster mode: bake text into the bitmap
    --no-background    vector mode: do not paint a black backdrop
    --password PWD     password for encrypted files
    --overwrite        replace existing output instead of skipping it
-p, --quiet            only report errors
```

Inputs may be files, folders, or shell globs. Existing outputs are skipped
unless `--overwrite` is given, a broken file does not stop the batch, and
darkpdf refuses to overwrite its own input.

## Notes

- Annotations, bookmarks, links and metadata are preserved; the original is
  never modified.
- Vector mode is not idempotent by design: running it on an already-dark
  document flips it back.
- The black backdrop is painted *under* the original content, so a document
  that paints its own background still shows that background (now inverted).
- A page whose content stream cannot be parsed, or which shares content with
  another page, is reported and left alone rather than risking damage.
- For reference, a 1161-page Adobe/BorisFX manual (6.4 MB, 1161 pages of text
  plus JPEG imagery) converts in about 3 seconds.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The suite builds its own fixtures (`tests/make_fixtures.py`) covering colour
operators, `q`/`Q` state, CMYK, form XObjects, tiling patterns, indexed and
JPEG images, shadings, shared content streams, encrypted files, and the CLI
itself, then checks the exact colours that come out.
