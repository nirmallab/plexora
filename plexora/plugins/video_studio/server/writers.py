"""Turning a sequence of RGBA frames into a file.

One interface, four implementations -- ffmpeg (MP4, WebM, ProRes, GIF),
PNG frames in a zip, a Pillow GIF and OpenCV's basic MP4 -- so the export job
neither knows nor cares which one it was given (`open_writer`).

Frames arrive as raw RGBA, `width x height x 4` bytes, in order. A writer
never holds more than one frame unless the format forces it (Pillow's GIF,
which is capped for exactly that reason).

**Colour.** Frames are sRGB. H.264 and VP9 are written as BT.709 limited
range, tagged so, which is what every player assumes for HD video; ProRes
keeps 10-bit 4:2:2 (4:4:4:4 with alpha). Nothing here changes a pixel's
value beyond that conversion.
"""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import zipfile
from collections import deque
from pathlib import Path

from plexora._subprocess import popen_kwargs

#: H.264 constant rate factor per quality preset; lower is better.
X264_CRF = {"draft": 23, "good": 20, "high": 17, "max": 14}
VP9_CRF = {"draft": 36, "good": 31, "high": 26, "max": 20}
#: Bitrate per quality at 1080p30, scaled by pixels x fps elsewhere.
BITRATE_1080P30 = {"draft": 4, "good": 8, "high": 14, "max": 24}

PILLOW_GIF_MAX_FRAMES = 300
PILLOW_GIF_MAX_SIDE = 960


class WriterError(Exception):
    """The encoder failed. Carries the last lines it printed."""

    def __init__(self, message, tail=""):
        super().__init__(message)
        self.tail = tail


def bitrate_mbps(quality: str, width: int, height: int, fps: int) -> float:
    base = BITRATE_1080P30.get(quality, 8)
    return round(base * (width * height) / (1920 * 1080) * max(1, fps) / 30, 2)


class FrameWriter:
    ext = ""

    def __init__(self, directory: Path, name: str, width: int, height: int, fps: int,
                 settings: dict):
        self.directory = Path(directory)
        self.name = name
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.settings = settings or {}
        self.frames = 0
        self.path = self.directory / f"{name}.{self.ext}"

    @property
    def frame_bytes(self) -> int:
        return self.width * self.height * 4

    def write(self, rgba) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> Path:  # pragma: no cover - interface
        raise NotImplementedError

    def abort(self) -> None:
        pass

    def describe(self) -> dict:
        return {"writer": type(self).__name__, "format": self.ext, "frames": self.frames}

    def _check(self, rgba):
        if len(rgba) != self.frame_bytes:
            raise WriterError(f"a frame of {len(rgba)} bytes; expected "
                              f"{self.width}x{self.height}x4 = {self.frame_bytes}")


class FfmpegWriter(FrameWriter):
    """ffmpeg as a subprocess reading raw RGBA on stdin."""

    def __init__(self, directory, name, width, height, fps, settings, *, ffmpeg: str,
                 encoders=(), fmt="mp4"):
        self.ext = {"mp4": "mp4", "webm": "webm", "mov": "mov", "gif": "gif"}[fmt]
        super().__init__(directory, name, width, height, fps, settings)
        self.fmt = fmt
        self.ffmpeg = ffmpeg
        self.encoders = set(encoders)
        self.codec = ""
        # MP4 is written fragmented first, so a run that fails halfway still
        # leaves a playable `partial.mp4`; `close` remuxes it with the index
        # at the front (faststart) for the finished file.
        self._target = (self.directory / f"partial.{self.ext}") if fmt == "mp4" else self.path
        self._log = tempfile.TemporaryFile(prefix="plexora-ffmpeg-")
        args = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{self.width}x{self.height}",
                "-framerate", str(self.fps), "-i", "-"]
        args += self._codec_args()
        args.append(str(self._target))
        self.args = args
        try:
            self._process = subprocess.Popen(args, stdin=subprocess.PIPE,
                                             stdout=subprocess.DEVNULL, stderr=self._log,
                                             **popen_kwargs())
        except OSError as exc:
            raise WriterError(f"could not start ffmpeg: {exc}") from exc

    def _codec_args(self) -> list[str]:
        quality = self.settings.get("quality", "good")
        transparent = bool(self.settings.get("transparent"))
        rate = self.settings.get("bitrate_mbps")
        gop = str(max(1, 2 * self.fps))
        tags = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
        if self.fmt == "mp4":
            # setparams stamps the primaries and transfer on the frames: the
            # bare -color_* options are lost behind the scale filter.
            scale = ["-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,"
                            "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709"]
            frag = ["-movflags", "+frag_keyframe+empty_moov+default_base_moof"]
            if "libx264" in self.encoders:
                self.codec = "libx264"
                rc = (["-b:v", f"{rate}M", "-maxrate", f"{rate * 1.5}M", "-bufsize", f"{rate * 2}M"]
                      if rate else ["-crf", str(X264_CRF.get(quality, 20))])
                return (scale + ["-c:v", "libx264", "-preset", "medium", "-profile:v", "high",
                                 "-g", gop] + rc + tags + frag)
            self.codec = "h264_videotoolbox"
            rate = rate or bitrate_mbps(quality, self.width, self.height, self.fps)
            return scale + ["-c:v", "h264_videotoolbox", "-b:v", f"{rate}M", "-g", gop] + tags + frag
        if self.fmt == "webm":
            self.codec = "libvpx-vp9"
            pix = "yuva420p" if transparent else "yuv420p"
            rc = (["-b:v", f"{rate}M"] if rate
                  else ["-crf", str(VP9_CRF.get(quality, 31)), "-b:v", "0"])
            return (["-c:v", "libvpx-vp9", "-pix_fmt", pix, "-row-mt", "1", "-deadline", "good",
                     "-cpu-used", "2", "-g", gop] + rc + tags)
        if self.fmt == "mov":
            self.codec = "prores_ks"
            if transparent:
                return ["-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuva444p10le",
                        "-alpha_bits", "16", "-vendor", "apl0"] + tags
            return ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le",
                    "-vendor", "apl0"] + tags
        self.codec = "gif"
        return ["-vf", "split[a][b];[a]palettegen=stats_mode=diff[p];"
                       "[b][p]paletteuse=dither=bayer:bayer_scale=4", "-loop", "0"]

    def write(self, rgba) -> None:
        self._check(rgba)
        try:
            self._process.stdin.write(rgba)
        except (BrokenPipeError, OSError) as exc:
            self._process.wait(timeout=10)
            raise WriterError(f"ffmpeg stopped: {exc}", self.tail()) from exc
        self.frames += 1

    def tail(self, lines=20) -> str:
        try:
            self._log.seek(0)
            text = self._log.read().decode("utf-8", "replace")
        except (OSError, ValueError):
            return ""
        return "\n".join(deque(text.splitlines(), maxlen=lines))

    def close(self) -> Path:
        try:
            self._process.stdin.close()
        except OSError:
            pass
        code = self._process.wait()
        if code != 0:
            raise WriterError(f"ffmpeg exited with status {code}", self.tail())
        if self.fmt == "mp4":
            remux = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i",
                 str(self._target), "-c", "copy", "-movflags", "+faststart", str(self.path)],
                capture_output=True, text=True, **popen_kwargs())
            if remux.returncode != 0:
                raise WriterError("could not finish the MP4 (remux failed)",
                                  "\n".join(remux.stderr.splitlines()[-20:]))
            self._target.unlink(missing_ok=True)
        self._log.close()
        return self.path

    def abort(self) -> None:
        try:
            self._process.stdin.close()
        except OSError:
            pass
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
        try:
            self._log.close()
        except OSError:
            pass

    def describe(self) -> dict:
        return {**super().describe(), "encoder": "ffmpeg", "codec": self.codec,
                "args": [a for a in self.args[1:] if a != str(self._target)]}


def _rgba_image(rgba, width, height):
    from PIL import Image  # noqa: PLC0415

    return Image.frombuffer("RGBA", (width, height), bytes(rgba), "raw", "RGBA", 0, 1)


class PngZipWriter(FrameWriter):
    """Every frame as a numbered PNG in one uncompressed zip -- lossless,
    always available, and what an editing suite imports as an image sequence."""

    ext = "zip"

    def __init__(self, directory, name, width, height, fps, settings):
        super().__init__(directory, name, width, height, fps, settings)
        self._zip = zipfile.ZipFile(self.path, "w", compression=zipfile.ZIP_STORED,
                                    allowZip64=True)

    def write(self, rgba) -> None:
        self._check(rgba)
        image = _rgba_image(rgba, self.width, self.height)
        if not self.settings.get("transparent"):
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, "PNG", compress_level=1)
        self.write_png(buffer.getvalue())

    def write_png(self, data: bytes) -> None:
        self._zip.writestr(f"{self.name}/frame_{self.frames:06d}.png", data)
        self.frames += 1

    def close(self) -> Path:
        self._zip.close()
        return self.path

    def abort(self) -> None:
        try:
            self._zip.close()
        except (OSError, ValueError):
            pass

    def describe(self) -> dict:
        return {**super().describe(), "encoder": "zipfile+Pillow", "codec": "PNG"}


class PillowGifWriter(FrameWriter):
    """A GIF without ffmpeg. Pillow needs every frame before it can write one,
    so this is capped: short clips only, and it says so."""

    ext = "gif"

    def __init__(self, directory, name, width, height, fps, settings):
        super().__init__(directory, name, width, height, fps, settings)
        if max(width, height) > PILLOW_GIF_MAX_SIDE:
            raise WriterError(f"a GIF without ffmpeg is limited to {PILLOW_GIF_MAX_SIDE} px "
                              f"on its longer side")
        self._frames = []

    def write(self, rgba) -> None:
        self._check(rgba)
        if len(self._frames) >= PILLOW_GIF_MAX_FRAMES:
            raise WriterError(f"a GIF without ffmpeg is limited to {PILLOW_GIF_MAX_FRAMES} "
                              f"frames")
        image = _rgba_image(rgba, self.width, self.height).convert("RGB")
        self._frames.append(image.quantize(colors=255, method=2, dither=1))
        self.frames += 1

    def close(self) -> Path:
        if not self._frames:
            raise WriterError("no frames")
        first, rest = self._frames[0], self._frames[1:]
        first.save(self.path, save_all=True, append_images=rest, loop=0,
                   duration=max(20, round(1000 / self.fps)), optimize=False, disposal=1)
        self._frames.clear()
        return self.path

    def describe(self) -> dict:
        return {**super().describe(), "encoder": "Pillow", "codec": "GIF"}


class Cv2Writer(FrameWriter):
    """OpenCV's VideoWriter, as probed by `encoders._probe_cv2`."""

    ext = "mp4"

    def __init__(self, directory, name, width, height, fps, settings, *, fourcc="mp4v"):
        super().__init__(directory, name, width, height, fps, settings)
        import cv2  # noqa: PLC0415

        self._cv2 = cv2
        self.fourcc = fourcc
        self._writer = cv2.VideoWriter(str(self.path), cv2.VideoWriter_fourcc(*fourcc),
                                       self.fps, (self.width, self.height))
        if not self._writer.isOpened():
            raise WriterError(f"OpenCV could not open a {fourcc} writer")

    def write(self, rgba) -> None:
        import numpy as np  # noqa: PLC0415

        self._check(rgba)
        frame = np.frombuffer(rgba, dtype=np.uint8).reshape(self.height, self.width, 4)
        self._writer.write(np.ascontiguousarray(frame[:, :, 2::-1]))
        self.frames += 1

    def close(self) -> Path:
        self._writer.release()
        return self.path

    def abort(self) -> None:
        try:
            self._writer.release()
        except Exception:  # noqa: BLE001
            pass

    def describe(self) -> dict:
        return {**super().describe(), "encoder": "OpenCV", "codec": self.fourcc}


def open_writer(entry: dict, directory: Path, name: str, width: int, height: int, fps: int,
                settings: dict) -> FrameWriter:
    """The writer an `encoders.report()` row names."""
    from plexora.plugins.video_studio.server import encoders  # noqa: PLC0415

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    writer = entry["writer"]
    if writer == "ffmpeg":
        ffmpeg = encoders.report()["ffmpeg"]
        return FfmpegWriter(directory, name, width, height, fps, settings,
                            ffmpeg=ffmpeg["path"], encoders=ffmpeg["encoders"],
                            fmt=entry["format"])
    if writer == "pngzip":
        return PngZipWriter(directory, name, width, height, fps, settings)
    if writer == "pillow":
        return PillowGifWriter(directory, name, width, height, fps, settings)
    if writer == "cv2":
        return Cv2Writer(directory, name, width, height, fps, settings,
                         fourcc=encoders.report()["cv2"].get("fourcc", "mp4v"))
    raise WriterError(f"no writer {writer!r}")


def file_size(path) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
