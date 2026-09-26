#!/usr/bin/env python3
"""darkpdf - turn white-background/black-text PDFs into dark-mode PDFs.

Two conversion strategies are available.

vector (default)
    Pages stay genuine vector content: text remains selectable and searchable
    and the files stay small.  Every colour operator in every content stream
    (page, form XObject, tiling pattern, Type 3 glyph) is rewritten from
    ``c`` to ``1 - c``, embedded images and shading functions are inverted too,
    and an opaque black backdrop is painted underneath each page.  Nothing is
    rasterised and the result renders identically in every viewer.

raster
    Each page is rendered to a bitmap, the pixels are inverted and the page is
    rebuilt from that image.  Larger and slower, but pixel-exact for content
    the recolourer cannot model (blend modes, soft masks, exotic colour
    spaces).  ``--keep-text`` adds an invisible text layer so the result stays
    searchable.

Examples
--------
    python darkpdf.py report.pdf
    python darkpdf.py report.pdf -o report_night.pdf -m raster -d 200
    python darkpdf.py papers/ -o converted/ --overwrite
    python darkpdf.py "scan_*.pdf" --suffix _night -f jpeg -q 80
"""

from __future__ import annotations

import argparse
import glob
import sys
import zlib
from pathlib import Path
from typing import Iterator, Sequence

import pikepdf

# Component counts of the colour operators, and the colour space each implies.
_GRAY, _RGB, _CMYK = "gray", "rgb", "cmyk"
_OPERAND_COUNT = {"g": 1, "rg": 3, "k": 4, "G": 1, "RG": 3, "K": 4}
_OPERAND_KIND = {
    "g": _GRAY, "rg": _RGB, "k": _CMYK,
    "G": _GRAY, "RG": _RGB, "K": _CMYK,
}
_FILL_OPS = frozenset({"g", "rg", "k", "sc", "scn"})
_OUTPUT_COMPONENTS = {1: _GRAY, 3: _RGB, 4: _CMYK}
_NAME_KINDS = {
    "/DeviceGray": _GRAY, "/G": _GRAY, "/CalGray": _GRAY,
    "/DeviceRGB": _RGB, "/RGB": _RGB, "/CalRGB": _RGB,
    "/DeviceCMYK": _CMYK, "/CMYK": _CMYK,
}

# Image codecs whose samples are not stored as plain bytes.
_PACKED_IMAGE_FILTERS = frozenset({
    "/DCTDecode", "/JPXDecode", "/JBIG2Decode", "/CCITTFaxDecode", "/CCF",
})
_MAX_DEPTH = 24


class ConversionError(RuntimeError):
    """Raised when a single document could not be converted."""


# --------------------------------------------------------------------------- #
# colour maths
# --------------------------------------------------------------------------- #
def _clamp(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def _cmyk_to_rgb(c: float, m: float, y: float, k: float) -> tuple[float, float, float]:
    return (
        _clamp(1.0 - min(1.0, c + k)),
        _clamp(1.0 - min(1.0, m + k)),
        _clamp(1.0 - min(1.0, y + k)),
    )


def _rgb_to_cmyk(r: float, g: float, b: float) -> tuple[float, float, float, float]:
    k = 1.0 - max(r, g, b)
    if k >= 1.0:
        return (0.0, 0.0, 0.0, 1.0)
    span = 1.0 - k
    return ((1.0 - r - k) / span, (1.0 - g - k) / span, (1.0 - b - k) / span, k)


def _invert_components(kind: str, values: Sequence[float]) -> list[float]:
    """Return the colour complement of *values* interpreted as *kind*."""
    if kind == _GRAY:
        return [_clamp(1.0 - values[0])]
    if kind == _RGB:
        return [_clamp(1.0 - v) for v in values[:3]]
    if kind == _CMYK:
        r, g, b = _cmyk_to_rgb(*values[:4])
        return [round(v, 6) for v in _rgb_to_cmyk(1.0 - r, 1.0 - g, 1.0 - b)]
    return list(values)


def _invert_bytes(data: bytes | bytearray) -> bytes:
    """Invert 8-bit samples in place (white <-> black, per component)."""
    return bytes(255 - b for b in data)


# --------------------------------------------------------------------------- #
# vector (recolour) conversion
# --------------------------------------------------------------------------- #
class _Recolorer:
    """Rewrites the colour operators of every content stream in a document."""

    def __init__(self, pdf: pikepdf.Pdf, *, background: bool = True) -> None:
        self.pdf = pdf
        self.background = background
        self._seen: set[tuple[int, int]] = set()
        self._page_cache: dict[tuple, bytes] = {}

    # -- entry point ------------------------------------------------------- #
    def run(self) -> None:
        for page in self.pdf.pages:
            self._do_page(page)

    # -- pages ------------------------------------------------------------- #
    def _do_page(self, page: pikepdf.Page) -> None:
        obj = page.obj
        resources = obj.get("/Resources")
        if resources is None:
            resources = pikepdf.Dictionary()
            obj["/Resources"] = resources

        contents = obj.get("/Contents")
        streams: list = []
        if isinstance(contents, pikepdf.Stream):
            streams = [contents]
        elif isinstance(contents, pikepdf.Array):
            streams = [s for s in contents if isinstance(s, pikepdf.Stream)]

        payload = self._recolor_page_content(streams, resources)
        if payload is None:
            # Unparseable or partially shared content: leave the page alone
            # rather than risk losing or double-inverting it.
            print("  ~ a page was left unchanged (unreadable content stream)",
                  file=sys.stderr)
            return

        prefix = self._backdrop(obj) if self.background else b""
        data = prefix + payload
        # The Stream constructor stores bytes verbatim, so compress here.
        stream = (
            pikepdf.Stream(
                self.pdf, zlib.compress(data),
                d={"/Filter": pikepdf.Name.FlateDecode},
            )
            if _flatable(data)
            else pikepdf.Stream(self.pdf, data)
        )
        obj["/Contents"] = self.pdf.make_indirect(stream)

    def _recolor_page_content(self, streams: list, resources) -> bytes | None:
        """Recolour a page's content streams, or None if that is not safe.

        A content stream shared by several pages is only converted once and
        the result reused, so no page ever gets it inverted twice.
        """
        if not streams:
            return b""

        key = tuple(s.objgen for s in streams)
        if key in self._page_cache:
            return self._page_cache[key]
        if any(s.objgen in self._seen for s in streams):
            return None  # shared with a differently grouped page

        operations: list = []
        try:
            for stream in streams:
                self._claim(stream)
                operations.extend(pikepdf.parse_content_stream(stream))
        except Exception:
            return None

        self._recolor_ops(operations, resources, 1)
        payload = pikepdf.unparse_content_stream(operations)
        self._page_cache[key] = payload
        return payload

    def _backdrop(self, page: pikepdf.Dictionary) -> bytes:
        """An opaque black rectangle covering the page.

        Pages that paint their own white background are unaffected (their
        background is recoloured to black and painted over this); pages that
        rely on the paper being white finally get a black background.
        """
        media = page.get("/MediaBox") or pikepdf.Array([0, 0, 612, 792])
        numbers = [float(n) for n in media]
        left, bottom = numbers[0], numbers[1]
        width, height = numbers[2] - numbers[0], numbers[3] - numbers[1]
        return (
            "q\n0 0 0 rg\n"
            f"{left:.4f} {bottom:.4f} {width:.4f} {height:.4f} re\nf\nQ\n"
        ).encode("ascii")

    # -- content stream ---------------------------------------------------- #
    def _recolor_ops(self, operations: list, resources, depth: int) -> None:
        """Rewrite *operations* in place with inverted colour components."""
        fill: list = [None, None]      # [kind, values]
        stroke: list = [None, None]
        stack: list[tuple[list, list]] = []
        result: list = []

        for instruction in operations:
            if isinstance(instruction, pikepdf.ContentStreamInlineImage):
                result.append(instruction)  # inline images are left alone
                continue

            operator = str(instruction.operator)
            operands = list(instruction.operands)
            new_operands: list | None = None

            if operator == "q":
                stack.append((fill[:], stroke[:]))
            elif operator == "Q":
                if stack:
                    fill, stroke = stack.pop()
            elif operator in ("cs", "CS"):
                kind = self._colorspace_kind(operands[0]) if operands else None
                if operator == "cs":
                    fill = [kind, None]
                else:
                    stroke = [kind, None]
            elif operator in _OPERAND_COUNT:
                values = _numbers(operands, _OPERAND_COUNT[operator])
                if values is not None:
                    kind = _OPERAND_KIND[operator]
                    new_operands = _invert_components(kind, values)
                    target = fill if operator in _FILL_OPS else stroke
                    target[0] = kind
                    target[1] = new_operands
            elif operator in ("sc", "scn", "SC", "SCN"):
                is_fill = operator in _FILL_OPS
                target = fill if is_fill else stroke
                kind = target[0] or _GRAY
                expected = {"gray": 1, "rgb": 3, "cmyk": 4}.get(kind, 0)
                values = _numbers(operands)
                if values is not None and len(values) == expected:
                    new_operands = _invert_components(kind, values)
                    target[1] = new_operands
            elif operator in ("d0", "d1"):
                values = _numbers(operands, 1)
                if values is not None:
                    new_operands = _invert_components(_GRAY, values)
            elif operator == "sh" and operands:
                shading = self._lookup(resources, "/Shading", operands[0])
                if shading is not None:
                    self._recolor_shading(shading)
            elif operator == "Do" and operands:
                xobject = self._lookup(resources, "/XObject", operands[0])
                if xobject is None:
                    result.append(instruction)
                    continue
                subtype = str(xobject.get("/Subtype", ""))
                if subtype == "/Form":
                    self._recolor_stream(xobject, resources, depth)
                elif subtype == "/Image":
                    self._invert_image(xobject)

            result.append(
                instruction if new_operands is None else
                pikepdf.ContentStreamInstruction(
                    new_operands, pikepdf.Operator(operator)
                )
            )

        operations[:] = result
        if depth < _MAX_DEPTH:
            self._recolor_resources(resources, depth)

    def _recolor_stream(self, stream, parent_resources, depth: int) -> None:
        """Recolour one subordinate content stream in place.

        Used for form XObjects, tiling patterns and Type 3 glyph procedures,
        which are all content streams with their own resource dictionary.
        """
        if not self._claim(stream):
            return
        try:
            operations = pikepdf.parse_content_stream(stream)
        except Exception:
            return
        resources = stream.get("/Resources") or parent_resources
        self._recolor_ops(operations, resources, depth + 1)
        stream.write(
            zlib.compress(pikepdf.unparse_content_stream(operations)),
            filter=pikepdf.Name.FlateDecode,
        )

    # -- resources --------------------------------------------------------- #
    def _recolor_resources(self, resources, depth: int) -> None:
        if not isinstance(resources, pikepdf.Dictionary):
            return

        for _, xobject in list(_items(resources, "/XObject")):
            subtype = str(xobject.get("/Subtype", ""))
            if subtype == "/Form":
                self._recolor_stream(xobject, resources, depth)
            elif subtype == "/Image":
                self._invert_image(xobject)

        for _, pattern in list(_items(resources, "/Pattern")):
            self._do_pattern(pattern, resources, depth)

        for _, shading in list(_items(resources, "/Shading")):
            self._recolor_shading(shading)

        for _, font in list(_items(resources, "/Font")):
            if str(font.get("/Subtype", "")) == "/Type3":
                charprocs = font.get("/CharProcs")
                if isinstance(charprocs, pikepdf.Dictionary):
                    glyph_resources = font.get("/Resources") or resources
                    for _, proc in list(charprocs.items()):
                        self._recolor_stream(proc, glyph_resources, depth)

    def _do_pattern(self, pattern, parent_resources, depth: int) -> None:
        if not isinstance(pattern, (pikepdf.Dictionary, pikepdf.Stream)):
            return
        try:
            pattern_type = int(pattern.get("/PatternType", -1))
        except (TypeError, ValueError):
            return
        if pattern_type == 2:
            shading = pattern.get("/Shading")
            self._recolor_shading(shading if shading is not None else pattern)
        elif pattern_type == 1 and isinstance(pattern, pikepdf.Stream):
            self._recolor_stream(pattern, parent_resources, depth)

    # -- shadings ---------------------------------------------------------- #
    def _recolor_shading(self, shading) -> None:
        if shading is None or not isinstance(shading, pikepdf.Dictionary):
            return
        if not self._claim(shading):
            return
        function = shading.get("/Function")
        if function is not None:
            self._recolor_function(function)
        functions = shading.get("/Functions")
        if isinstance(functions, pikepdf.Array):
            for entry in functions:
                self._recolor_function(entry)

    def _recolor_function(self, function) -> None:
        """Invert the colours a PDF function maps its inputs to.

        Only the function types whose colour endpoints are enumerated are
        handled: exponential (2), stitching (3) and sampled (0).
        """
        if not isinstance(function, pikepdf.Dictionary):
            return  # a bare name, or something we cannot introspect
        if not self._claim(function):
            return
        try:
            function_type = int(function.get("/FunctionType", -1))
        except (TypeError, ValueError):
            return

        if function_type in (2, 3):
            low, high = function.get("/C0"), function.get("/C1")
            if not (isinstance(low, pikepdf.Array) and isinstance(high, pikepdf.Array)):
                return
            kind = _OUTPUT_COMPONENTS.get(len(low))
            if kind is None:
                return
            function["/C0"] = pikepdf.Array(
                _invert_components(kind, [float(v) for v in low])
            )
            function["/C1"] = pikepdf.Array(
                _invert_components(kind, [float(v) for v in high])
            )
        elif function_type == 0:
            size = function.get("/Size")
            if not isinstance(size, pikepdf.Array) or len(size) < 2:
                return
            outputs = int(size[0])
            if _OUTPUT_COMPONENTS.get(outputs) is None:
                return
            if int(function.get("/BitsPerSample", 8)) != 8:
                return
            data = (
                function if isinstance(function, pikepdf.Stream)
                else (function.get("/EncodedData") or function.get("/Data"))
            )
            if not isinstance(data, pikepdf.Stream):
                return
            length = outputs * int(size[1])
            raw = data.read_bytes()
            if len(raw) < length:
                return
            data.write(
                zlib.compress(_invert_bytes(raw[:length])),
                filter=pikepdf.Name.FlateDecode,
            )

    # -- images ------------------------------------------------------------ #
    def _invert_image(self, image) -> None:
        if not isinstance(image, pikepdf.Stream) or not self._claim(image):
            return
        if image.get("/ImageMask"):
            return  # stencil mask, no colour to invert

        kind = self._colorspace_kind(image.get("/ColorSpace"))
        if kind is None:
            return

        if kind == "indexed":
            self._invert_palette(image)
            return

        filters = _filter_names(image)
        if filters & _PACKED_IMAGE_FILTERS:
            # JPEG/JPX/JBIG2/CCITT samples are not plain bytes: the only way in
            # is to decode the image, so this needs Pillow.
            if "/DCTDecode" in filters:
                self._invert_jpeg(image, kind)
            return
        if kind == _CMYK:
            return  # 8-bit CMYK samples would need real conversion
        if int(image.get("/BitsPerComponent", 8)) != 8:
            return
        if not _decode_is_identity(image, components=1 if kind == _GRAY else 3):
            return

        raw = image.read_bytes()
        if raw:
            image.write(
                zlib.compress(_invert_bytes(raw)), filter=pikepdf.Name.FlateDecode
            )

    def _invert_jpeg(self, image, kind: str) -> None:
        """Decode a JPEG image, invert it and store it back as a JPEG."""
        try:
            from io import BytesIO

            from PIL import Image, ImageOps
        except ImportError:  # pragma: no cover - Pillow is a hard dependency
            return

        payload = _encoded_image_bytes(image)
        if payload is None:
            return
        try:
            with Image.open(BytesIO(payload)) as source:
                source.load()
                gray = kind == _GRAY
                converted = source.convert("L" if gray else "RGB")
                buffer = BytesIO()
                ImageOps.invert(converted).save(
                    buffer, "JPEG", quality=95, optimize=True
                )
        except Exception:
            return  # a JPEG we cannot decode is left as it is

        image.write(buffer.getvalue(), filter=pikepdf.Name.DCTDecode)
        image["/ColorSpace"] = pikepdf.Name(
            "/DeviceGray" if gray else "/DeviceRGB"
        )
        image["/BitsPerComponent"] = 8
        if "/DecodeParms" in image:
            del image["/DecodeParms"]

    def _invert_palette(self, image) -> None:
        """Invert an indexed image by flipping its palette.

        The index values are left alone, so this works at any bit depth and is
        unaffected by /Decode (which maps indices, not palette entries).
        """
        space = image.get("/ColorSpace")
        if not isinstance(space, pikepdf.Array) or len(space) < 4:
            return
        palette = space[3]
        try:
            if isinstance(palette, pikepdf.Stream):
                palette.write(
                    zlib.compress(_invert_bytes(palette.read_bytes())),
                    filter=pikepdf.Name.FlateDecode,
                )
            elif isinstance(palette, pikepdf.String):
                space[3] = pikepdf.String(_invert_bytes(bytes(palette)))
        except Exception:
            return  # best effort: an odd palette is not worth failing over

    # -- helpers ----------------------------------------------------------- #
    def _claim(self, obj) -> bool:
        """Return True the first time *obj* is seen (cycle protection)."""
        try:
            key = obj.objgen
        except Exception:
            return True
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    def _lookup(self, resources, category, name):
        if not isinstance(resources, pikepdf.Dictionary):
            return None
        table = resources.get(category)
        if not isinstance(table, pikepdf.Dictionary):
            return None
        if not isinstance(name, pikepdf.Name):
            return None
        return table.get(str(name))

    def _colorspace_kind(self, cs, depth: int = 0) -> str | None:
        """Classify a colour space as gray/rgb/cmyk/indexed, or None."""
        if cs is None or depth > 6:
            return None

        if isinstance(cs, pikepdf.Name):
            return _NAME_KINDS.get(str(cs))

        if isinstance(cs, pikepdf.Array):
            family = str(cs[0]) if len(cs) else ""
            if family == "/Indexed":
                return "indexed"
            if family in ("/Pattern", "/Separation", "/DeviceN", "/Lab"):
                return None
            if family == "/ICCBased":
                profile = cs[1] if len(cs) > 1 else None
                components = (
                    int(profile.get("/N", 0))
                    if isinstance(profile, pikepdf.Stream) else 0
                )
                return {1: _GRAY, 3: _RGB, 4: _CMYK}.get(components)
            return self._colorspace_kind(cs[0], depth + 1) if len(cs) else None

        if isinstance(cs, pikepdf.Dictionary):
            family = str(cs.get("/Family", "")) or None
            if family:
                return _NAME_KINDS.get(family)
            for key in ("/CalRGB", "/CalGray", "/Lab"):
                if key in cs:
                    return _NAME_KINDS.get(key)
        return None


def _numbers(operands, expected: int | None = None) -> list[float] | None:
    """Return *operands* as floats, or None if they are not all numbers."""
    if expected is not None and len(operands) != expected:
        return None
    values: list[float] = []
    for operand in operands:
        if isinstance(operand, (pikepdf.Name, pikepdf.String, pikepdf.Array,
                                pikepdf.Dictionary)):
            return None
        try:
            values.append(float(operand))
        except (TypeError, ValueError):
            return None
    return values or None


def _items(resources, category) -> Iterator[tuple[str, object]]:
    table = resources.get(category) if isinstance(resources, pikepdf.Dictionary) else None
    if isinstance(table, pikepdf.Dictionary):
        for key in list(table.keys()):
            yield key, table[key]


def _filter_list(stream) -> list[str]:
    """The filters a stream is encoded with, in the order they must be undone."""
    filters = stream.get("/Filter")
    if isinstance(filters, pikepdf.Name):
        return [str(filters)]
    if isinstance(filters, pikepdf.Array):
        return [str(f) for f in filters]
    return []


def _filter_names(stream) -> set[str]:
    """The names of every filter a stream is encoded with."""
    return set(_filter_list(stream))


def _encoded_image_bytes(stream) -> bytes | None:
    """The image's own encoded bytes, with the plain filters peeled off.

    Image data may sit behind ordinary filters (``/Filter [/FlateDecode
    /DCTDecode]`` is common), and no PDF library will hand back the JPEG in
    that case.  Returns None when the chain uses something we do not
    reimplement, so the caller can leave the image alone.
    """
    data = stream.read_raw_bytes()
    parms = stream.get("/DecodeParms") or stream.get("/DP")
    for index, name in enumerate(_filter_list(stream)):
        if name in _PACKED_IMAGE_FILTERS:
            return data
        if name != "/FlateDecode":
            return None  # LZW, RunLength, ASCII85 ... not worth reimplementing
        if _predicts(parms, index):
            return None  # a PNG-style predictor would need undoing first
        try:
            data = zlib.decompress(data)
        except zlib.error:
            return None
    return data


def _predicts(parms, index: int) -> bool:
    """True if the *index*-th filter of a chain uses a predictor."""
    if isinstance(parms, pikepdf.Array):
        parms = parms[index] if index < len(parms) else None
    if not isinstance(parms, pikepdf.Dictionary):
        return False
    try:
        return int(parms.get("/Predictor", 1)) > 1
    except (TypeError, ValueError):
        return True


def _decode_is_identity(image, components: int) -> bool:
    """True unless /Decode already flips or remaps the samples."""
    decode = image.get("/Decode")
    if decode is None:
        return True
    try:
        values = [float(v) for v in decode]
    except (TypeError, ValueError):
        return False
    if len(values) < components * 2:
        return False
    for index in range(components):
        low, high = values[index * 2], values[index * 2 + 1]
        if abs(low - 0.0) > 1e-6 or abs(high - 1.0) > 1e-6:
            return False
    return True


def _flatable(data: bytes) -> bool:
    return len(zlib.compress(data)) < len(data)


def invert_vector(
    src: Path,
    dst: Path,
    *,
    password: str | None = None,
    background: bool = True,
) -> int:
    """Convert *src* to *dst* by recolouring.  Returns the page count."""
    with pikepdf.open(src, password=password or "") as pdf:
        if not len(pdf.pages):
            raise ConversionError("document has no pages")
        _Recolorer(pdf, background=background).run()
        pages = len(pdf.pages)
        _write(pdf, dst)
        return pages


# --------------------------------------------------------------------------- #
# raster conversion
# --------------------------------------------------------------------------- #
def _invisible_text_layer(target_page, source_page) -> None:
    """Copy *source_page*'s text onto *target_page* as invisible text.

    Render mode 3 (invisible) keeps words selectable and searchable while the
    inverted bitmap underneath stays visible.  Characters the base-14 font
    cannot encode are dropped - they would only garble copied text.
    """
    import pymupdf

    font = pymupdf.Font("helv")

    def clean(text: str) -> str:
        return "".join(
            ch for ch in text
            if ch.isspace() or font.has_glyph(ord(ch))
        )

    writer = pymupdf.TextWriter(target_page.rect)
    added = 0
    for block in source_page.get_text("dict").get("blocks", ()):
        for line in block.get("lines", ()):
            for span in line.get("spans", ()):
                text = clean(span.get("text", "")).strip()
                size = float(span.get("size", 0) or 0)
                origin = span.get("origin") or (0, 0)
                if not text or size <= 0:
                    continue
                try:
                    writer.append(
                        (float(origin[0]), float(origin[1])),
                        text, font=font, fontsize=min(max(size, 1.0), 200.0),
                    )
                    added += 1
                except Exception:  # pragma: no cover - defensive
                    continue
    if added:
        writer.write_text(target_page, render_mode=3, overlay=True)


def _encode_image(image, image_format: str, quality: int) -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    if image_format == "jpeg":
        image.convert("RGB").save(buffer, "JPEG", quality=quality, optimize=True)
    else:
        image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


def invert_raster(
    src: Path,
    dst: Path,
    *,
    dpi: int = 200,
    password: str | None = None,
    keep_text: bool = True,
    image_format: str = "png",
    quality: int = 90,
) -> int:
    """Convert *src* to *dst* by inverting rendered pixels.  Page count."""
    import pymupdf
    from PIL import Image, ImageOps

    document = pymupdf.open(src)
    try:
        if document.needs_pass and not document.authenticate(password or ""):
            raise ConversionError("wrong or missing password")
        if document.page_count == 0:
            raise ConversionError("document has no pages")

        out = pymupdf.open()
        try:
            for page in document:
                pixmap = page.get_pixmap(dpi=dpi, alpha=False)
                image = Image.frombytes(
                    "RGB", (pixmap.width, pixmap.height), pixmap.samples
                )
                rect = page.rect
                new_page = out.new_page(width=rect.width, height=rect.height)
                new_page.insert_image(
                    new_page.rect,
                    stream=_encode_image(
                        ImageOps.invert(image), image_format, quality
                    ),
                    keep_proportion=False,
                )
                if keep_text:
                    try:
                        _invisible_text_layer(new_page, page)
                    except Exception as exc:  # pragma: no cover - defensive
                        print(f"  ~ text layer skipped: {exc}", file=sys.stderr)

            out.set_metadata({
                k: v for k, v in (document.metadata or {}).items()
                if isinstance(v, str)
            })
            dst.parent.mkdir(parents=True, exist_ok=True)
            out.save(dst, garbage=4, deflate=True)
            return out.page_count
        finally:
            out.close()
    finally:
        document.close()


# --------------------------------------------------------------------------- #
# input discovery and output
# --------------------------------------------------------------------------- #
def iter_input_files(paths: Sequence[str]) -> Iterator[Path]:
    """Yield every PDF to convert from *paths* (files, dirs, or glob patterns)."""
    seen: set[Path] = set()
    for raw in paths:
        candidate = Path(raw)
        if candidate.is_dir():
            items = sorted(
                p for p in candidate.iterdir()
                if p.is_file() and p.suffix.lower() == ".pdf"
            )
            if not items:
                print(f"  ! no PDFs found in {candidate}", file=sys.stderr)
        elif candidate.is_file():
            items = [candidate]
        else:
            items = sorted(
                p for p in (Path(m) for m in glob.glob(raw))
                if p.is_file() and p.suffix.lower() == ".pdf"
            )
            if not items:
                print(f"  ! no match for: {raw}", file=sys.stderr)
        for path in items:
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield path


def output_path_for(src: Path, out_arg: str | None, suffix: str) -> Path:
    """Work out where the dark copy of *src* should be written."""
    target_dir = Path(out_arg) if out_arg else src.parent
    return target_dir / f"{src.stem}{suffix}.pdf"


def _write(pdf: pikepdf.Pdf, dst: Path) -> None:
    """Save *pdf* to *dst*, then verify the result is a readable PDF."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(
        dst,
        compress_streams=True,
        object_stream_mode=pikepdf.ObjectStreamMode.generate,
        recompress_flate=True,
    )
    with pikepdf.open(dst) as check:
        if not len(check.pages):
            raise ConversionError("output contains no pages")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _examples() -> str:
    """The Examples block of the module docstring, formatted for --help."""
    lines = __doc__.split("Examples")[-1].strip().splitlines()
    body = [line.strip() for line in lines if line.strip().strip("-")]
    return "examples:\n  " + "\n  ".join(body)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="darkpdf",
        description="Invert a PDF: white background/black text becomes "
                    "black background/white text.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_examples(),
    )
    parser.add_argument(
        "inputs", nargs="+", help="PDF file(s), directory of PDFs, or glob",
    )
    parser.add_argument(
        "-o", "--output", metavar="PATH",
        help="output PDF (single input) or output directory (batch)",
    )
    parser.add_argument(
        "-m", "--mode", choices=("vector", "raster"), default="vector",
        help="conversion strategy (default: vector)",
    )
    parser.add_argument(
        "-d", "--dpi", type=int, default=200, metavar="N",
        help="render resolution for raster mode (default: 200)",
    )
    parser.add_argument(
        "-f", "--format", dest="image_format", choices=("png", "jpeg"),
        default="png", help="image codec for raster mode (default: png)",
    )
    parser.add_argument(
        "-q", "--quality", type=int, default=90, metavar="N",
        help="JPEG quality 1-100, raster mode only (default: 90)",
    )
    parser.add_argument(
        "--suffix", default="_dark", metavar="TEXT",
        help="suffix for generated output names (default: _dark)",
    )
    parser.add_argument(
        "--keep-text", dest="keep_text", action="store_true", default=True,
        help="raster mode: keep a searchable text layer (default)",
    )
    parser.add_argument(
        "--no-keep-text", dest="keep_text", action="store_false",
        help="raster mode: bake text into the bitmap",
    )
    parser.add_argument(
        "--no-background", dest="background", action="store_false",
        default=True,
        help="vector mode: do not paint a black backdrop",
    )
    parser.add_argument(
        "--password", metavar="PWD", help="password for encrypted files",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="replace existing output files instead of skipping them",
    )
    parser.add_argument(
        "-p", "--quiet", action="store_true", help="only report errors",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    say = (lambda *a: None) if args.quiet else (lambda *a: print(*a))

    if args.mode == "raster":
        if not 36 <= args.dpi <= 2400:
            print("error: --dpi must be between 36 and 2400", file=sys.stderr)
            return 2
        if not 1 <= args.quality <= 100:
            print("error: --quality must be between 1 and 100", file=sys.stderr)
            return 2

    files = list(iter_input_files(args.inputs))
    if not files:
        print("error: no input PDFs", file=sys.stderr)
        return 2

    single = len(files) == 1
    as_file = single and bool(args.output) and Path(args.output).suffix.lower() == ".pdf"

    failures = 0
    for src in files:
        dst = Path(args.output) if as_file else output_path_for(
            src, args.output, args.suffix
        )
        if dst.resolve() == src.resolve():
            print(f"  ! {src.name}: refusing to overwrite the input; "
                  f"choose another -o path", file=sys.stderr)
            failures += 1
            continue
        if dst.exists() and not args.overwrite:
            say(f"  - skipped (exists): {dst.name}")
            continue

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if args.mode == "vector":
                pages = invert_vector(
                    src, dst, password=args.password,
                    background=args.background,
                )
            else:
                pages = invert_raster(
                    src, dst, dpi=args.dpi, password=args.password,
                    keep_text=args.keep_text,
                    image_format=args.image_format, quality=args.quality,
                )
            say(f"  + {src.name} -> {dst}  ({pages} page(s), {args.mode})")
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
            print(f"  ! {src.name}: {exc}", file=sys.stderr)
            failures += 1

    if failures:
        print(f"{failures} file(s) failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
