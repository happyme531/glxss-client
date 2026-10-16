"""Native YV12 conversion/scaling, with a portable Pillow fallback."""

import ctypes as ct
from ctypes.util import find_library

from PIL import Image

from glxss import yv12_image


class FrameConverter:
    def __init__(self):
        self.context = None
        self.key = None
        self.library = None
        try:
            self.library = lib = ct.CDLL(find_library("swscale") or "libswscale.so")
            util = ct.CDLL(find_library("avutil") or "libavutil.so")
            util.av_get_pix_fmt.argtypes = [ct.c_char_p]
            util.av_get_pix_fmt.restype = ct.c_int
            self.yuv_format = util.av_get_pix_fmt(b"yuv420p")
            self.rgb_format = util.av_get_pix_fmt(b"bgra")
            lib.sws_getContext.argtypes = [ct.c_int] * 7 + [ct.c_void_p] * 3
            lib.sws_getContext.restype = ct.c_void_p
            lib.sws_scale.argtypes = [
                ct.c_void_p,
                ct.POINTER(ct.c_void_p),
                ct.POINTER(ct.c_int),
                ct.c_int,
                ct.c_int,
                ct.POINTER(ct.c_void_p),
                ct.POINTER(ct.c_int),
            ]
            lib.sws_scale.restype = ct.c_int
            lib.sws_freeContext.argtypes = [ct.c_void_p]
            lib.sws_freeContext.restype = None
            lib.sws_getCoefficients.argtypes = [ct.c_int]
            lib.sws_getCoefficients.restype = ct.POINTER(ct.c_int)
            lib.sws_setColorspaceDetails.argtypes = [
                ct.c_void_p,
                ct.POINTER(ct.c_int),
                ct.c_int,
                ct.POINTER(ct.c_int),
                ct.c_int,
                ct.c_int,
                ct.c_int,
                ct.c_int,
            ]
            lib.sws_setColorspaceDetails.restype = ct.c_int
        except (OSError, AttributeError):
            self.library = None

    @property
    def backend(self):
        return "FFmpeg swscale" if self.library else "Pillow"

    def convert(self, data, width, height, limit=None):
        if width % 2 or height % 2 or len(data) != width * height * 3 // 2:
            raise ValueError("Incomplete or invalid YV12 frame")
        factor = min(1.0, limit[0] / width, limit[1] / height) if limit else 1.0
        target = (
            max(2, int(width * factor) // 2 * 2),
            max(2, int(height * factor) // 2 * 2),
        )
        if self.library is None:
            image = yv12_image(data, width, height, target)
            return image.tobytes("raw", "BGRX"), *target
        key = (width, height, *target)
        if key != self.key:
            self.close()
            self.context = self.library.sws_getContext(
                width,
                height,
                self.yuv_format,
                *target,
                self.rgb_format,
                2 | 0x40000,
                None,
                None,
                None,
            )
            if not self.context:
                raise RuntimeError("Cannot create video conversion context")
            coefficients = self.library.sws_getCoefficients(
                5
            )  # ITU BT.601, limited YUV -> full RGB.
            self.library.sws_setColorspaceDetails(
                self.context, coefficients, 0, coefficients, 1, 0, 1 << 16, 1 << 16
            )
            self.key = key
            # SIMD converters may read a little past the final source plane.
            self.source = ct.create_string_buffer(len(data) + 64)
            self.output = ct.create_string_buffer(target[0] * target[1] * 4 + 64)
        ct.memmove(self.source, data, len(data))
        address = ct.addressof(self.source)
        pixels = width * height
        # Firmware is Y,V,U; swscale expects Y,U,V.
        source_planes = (ct.c_void_p * 4)(
            address, address + pixels * 5 // 4, address + pixels, None
        )
        source_strides = (ct.c_int * 4)(width, width // 2, width // 2, 0)
        dest_planes = (ct.c_void_p * 4)(ct.addressof(self.output), None, None, None)
        dest_strides = (ct.c_int * 4)(target[0] * 4, 0, 0, 0)
        rows = self.library.sws_scale(
            self.context,
            source_planes,
            source_strides,
            0,
            height,
            dest_planes,
            dest_strides,
        )
        if rows != target[1]:
            raise RuntimeError("Incomplete video conversion")
        return memoryview(self.output).cast("B")[: target[0] * target[1] * 4], *target

    def image(self, data, width, height, limit=None):
        bgra, w, h = self.convert(data, width, height, limit)
        return Image.frombytes("RGB", (w, h), bytes(bgra), "raw", "BGRX")

    def close(self):
        if self.context:
            self.library.sws_freeContext(self.context)
            self.context = None
        self.key = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
