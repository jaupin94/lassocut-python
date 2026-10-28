"""HTTP client for the LassoCut background-removal API. Standard library only (urllib)."""
from __future__ import annotations

import json
import os
import time
import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Callable, Dict, List, Mapping, Optional, Tuple, Union
from urllib import error as urlerror
from urllib import request as urlrequest

from ._version import __version__

DEFAULT_API_URL = "https://api.lassocut.com/v1.0"
PRESETS = ("amazon", "ebay", "etsy", "shopify", "zalando", "instagram")
SHADOW_LENGTHS = ("short", "medium", "long")
# Options a preset decides itself: never sent together with a preset.
PRESET_BLOCKED = ("format", "channels", "bg_color", "bg_image", "bg_image_url", "crop", "crop_margin", "scale", "position")
RETRY_STATUSES = frozenset({502, 503, 504})
CLIENT_NAME = "python-sdk"   # X-Lassocut-Client: which integration brings the calls
USER_AGENT = f"lassocut-python/{__version__}"

ImageInput = Union[str, "os.PathLike[str]", bytes, bytearray, memoryview, IO[bytes]]
_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "application/zip": ".zip"}


class LassoCutError(Exception):
    """An API call failed. ``status`` is the HTTP status (0 when the API could not be reached)."""

    def __init__(self, message: str, status: int = 0, code: Optional[str] = None,
                 title: Optional[str] = None, detail: Optional[str] = None,
                 retry_after: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.retry_after = retry_after


class LassoCutWarning(UserWarning):
    """An option was ignored (for example a shadow with the zalando preset)."""


@dataclass
class Result:
    """The image returned by ``remove_background`` and the response headers that describe it."""

    data: bytes
    headers: Dict[str, str] = field(default_factory=dict)   # lower-case names

    def __bytes__(self) -> bytes:
        return self.data

    def _int(self, name: str) -> Optional[int]:
        value = self.headers.get(name)
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip()

    @property
    def extension(self) -> str:
        """File extension matching the returned format: .png, .jpg, .webp or .zip."""
        return _EXTENSIONS.get(self.content_type, ".png")

    @property
    def credits_charged(self) -> float:
        try:
            return float(self.headers.get("x-credits-charged", "0") or 0)
        except ValueError:
            return 0.0

    @property
    def width(self) -> Optional[int]:
        return self._int("x-width")

    @property
    def height(self) -> Optional[int]:
        return self._int("x-height")

    @property
    def type(self) -> Optional[str]:
        """Detected or requested subject type (X-Type)."""
        return self.headers.get("x-type")

    @property
    def foreground(self) -> Optional[Tuple[int, int, int, int]]:
        """Bounding box of the subject in the returned image: (top, left, width, height)."""
        box = [self._int(f"x-foreground-{k}") for k in ("top", "left", "width", "height")]
        return None if None in box else (box[0], box[1], box[2], box[3])  # type: ignore[return-value]

    @property
    def preset(self) -> Optional[str]:
        return self.headers.get("x-preset")

    @property
    def compliance(self) -> Optional[str]:
        """With a preset: ``ok``, or ``fail; reasons=...``. Compliant means compliant with the image rules LassoCut checks."""
        return self.headers.get("x-compliance")

    @property
    def compliant(self) -> Optional[bool]:
        value = self.compliance
        return None if value is None else value.strip().lower() == "ok"

    @property
    def compliance_reasons(self) -> List[str]:
        value = (self.compliance or "").strip()
        if not value or value.lower() == "ok":
            return []
        reasons = value.split("reasons=", 1)[1] if "reasons=" in value else value.split(";", 1)[-1]
        return [r.strip() for r in reasons.split(",") if r.strip() and r.strip().lower() != "fail"]

    @property
    def preset_ignored(self) -> List[str]:
        return [f.strip() for f in self.headers.get("x-preset-ignored", "").split(",") if f.strip()]

    @property
    def upscaled(self) -> bool:
        return self.headers.get("x-upscaled", "").lower() == "true"

    def save(self, path: Union[str, "os.PathLike[str]"]) -> Path:
        out = Path(path)
        out.write_bytes(self.data)
        return out


@dataclass
class Balance:
    """Account balance from ``GET /account``."""

    credits: float
    free_previews: int
    raw: Dict[str, Any] = field(default_factory=dict)


class LassoCut:
    """Client for the LassoCut API.

    >>> client = LassoCut()   # reads LASSOCUT_API_KEY
    >>> client.remove_background_file("photo.jpg", "photo-cutout.png")
    """

    def __init__(self, api_key: Optional[str] = None, *, api_url: Optional[str] = None,
                 timeout: float = 180.0, max_attempts: int = 3, backoff: float = 1.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        key = api_key or os.environ.get("LASSOCUT_API_KEY") or os.environ.get("REMOVE_BG_API_KEY")
        if not key:
            raise LassoCutError("No API key: pass api_key or set LASSOCUT_API_KEY. "
                                "Get one at https://www.lassocut.com/account/")
        self.api_key = key
        self.api_url = (api_url or os.environ.get("LASSOCUT_API_URL") or DEFAULT_API_URL).rstrip("/")
        self.timeout = timeout
        self.max_attempts = max(1, int(max_attempts))
        self.backoff = backoff
        self._sleep = sleep

    def __repr__(self) -> str:   # never show the key
        return f"LassoCut(api_url={self.api_url!r})"

    # -- public API ---------------------------------------------------------------------------------------------

    def remove_background(self, image: Optional[ImageInput] = None, *, image_url: Optional[str] = None,
                          size: str = "auto", type: Optional[str] = None, format: Optional[str] = None,
                          channels: Optional[str] = None, bg_color: Optional[str] = None,
                          bg_image_url: Optional[str] = None, bg_image: Optional[ImageInput] = None,
                          crop: Optional[bool] = None, crop_margin: Optional[str] = None,
                          scale: Optional[str] = None, position: Optional[str] = None,
                          preset: Optional[str] = None, shadow: Optional[str] = None,
                          light_angle: Optional[int] = None, shadow_length: Optional[str] = None,
                          shadow_strength: Optional[int] = None,
                          extra: Optional[Mapping[str, Any]] = None) -> Result:
        """Remove the background of one image and return the result.

        Pass exactly one of ``image`` (a path, bytes or a binary file object) and ``image_url``.
        ``preset`` is one of amazon, ebay, etsy, shopify, zalando, instagram: it decides the format, background
        and framing, so it cannot be combined with format, channels, bg_color, bg_image, bg_image_url, crop,
        crop_margin, scale or position. ``shadow="cast"`` adds a cast shadow (``light_angle`` -90..90, positive =
        light from the left; ``shadow_length`` short, medium or long; ``shadow_strength`` 0..100); zalando allows
        no shadow, so it is ignored there with a LassoCutWarning. ``extra`` passes any other API field.
        """
        fields, files = build_request(
            image=image, image_url=image_url, size=size, type=type, format=format, channels=channels,
            bg_color=bg_color, bg_image_url=bg_image_url, bg_image=bg_image, crop=crop, crop_margin=crop_margin,
            scale=scale, position=position, preset=preset, shadow=shadow, light_angle=light_angle,
            shadow_length=shadow_length, shadow_strength=shadow_strength, extra=extra)
        body, content_type = _multipart(fields, files)
        # One key for every attempt: a retried call is charged once.
        headers = {"Content-Type": content_type, "Idempotency-Key": str(uuid.uuid4())}
        status, resp_headers, data = self._send("POST", "/removebg", body, headers)
        return Result(data=data, headers=resp_headers)

    def remove_background_file(self, input_path: Union[str, "os.PathLike[str]"],
                               output_path: Union[str, "os.PathLike[str]", None] = None, **options: Any) -> Result:
        """Remove the background of a file and write the result.

        ``output_path`` defaults to ``<name>-lassocut<ext>`` next to the input, the extension following the
        returned format. Takes the same options as ``remove_background``.
        """
        src = Path(input_path)
        result = self.remove_background(src, **options)
        out = Path(output_path) if output_path is not None else src.with_name(f"{src.stem}-lassocut{result.extension}")
        if out.parent and not out.parent.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
        result.save(out)
        return result

    def credits(self) -> Balance:
        """The account balance: paid credits and the free previews available now. Free of charge."""
        _, _, data = self._send("GET", "/account", None, {})
        payload = json.loads(data.decode("utf-8"))
        attrs = payload["data"]["attributes"]
        return Balance(credits=float(attrs["credits"]["total"]),
                       free_previews=int(attrs.get("api", {}).get("free_calls", 0)), raw=attrs)

    # -- transport ----------------------------------------------------------------------------------------------

    def _send(self, method: str, path: str, body: Optional[bytes],
              headers: Mapping[str, str]) -> Tuple[int, Dict[str, str], bytes]:
        all_headers = {"X-Api-Key": self.api_key, "X-Lassocut-Client": CLIENT_NAME, "User-Agent": USER_AGENT,
                       **headers}
        url = self.api_url + path
        attempt = 0
        while True:
            attempt += 1
            req = urlrequest.Request(url, data=body, headers=all_headers, method=method)
            try:
                with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                    return resp.status, _lower(resp.headers.items()), resp.read()
            except urlerror.HTTPError as e:
                status, resp_headers = e.code, _lower(e.headers.items() if e.headers else [])
                payload = e.read() if e.fp is not None else b""
                if status in RETRY_STATUSES and attempt < self.max_attempts:
                    self._sleep(self.backoff * attempt)
                    continue
                raise _error_for(status, resp_headers, payload) from None
            except (urlerror.URLError, OSError) as e:
                reason = getattr(e, "reason", e)
                raise LassoCutError(f"Could not reach LassoCut ({reason}), check the connection.") from None


def build_request(*, image: Optional[ImageInput] = None, image_url: Optional[str] = None, size: str = "auto",
                  type: Optional[str] = None, format: Optional[str] = None, channels: Optional[str] = None,
                  bg_color: Optional[str] = None, bg_image_url: Optional[str] = None,
                  bg_image: Optional[ImageInput] = None, crop: Optional[bool] = None,
                  crop_margin: Optional[str] = None, scale: Optional[str] = None, position: Optional[str] = None,
                  preset: Optional[str] = None, shadow: Optional[str] = None, light_angle: Optional[int] = None,
                  shadow_length: Optional[str] = None, shadow_strength: Optional[int] = None,
                  extra: Optional[Mapping[str, Any]] = None,
                  ) -> Tuple[Dict[str, str], Dict[str, Tuple[str, bytes]]]:
    """Validate the options and return (form fields, files). Raises ValueError on a bad combination."""
    if (image is None) == (image_url is None):
        raise ValueError("pass exactly one of image and image_url")
    fields: Dict[str, str] = {"size": str(size)}
    files: Dict[str, Tuple[str, bytes]] = {}
    if image is not None:
        files["image_file"] = _read_image(image, "image")
    else:
        fields["image_url"] = str(image_url)
    if type is not None:
        fields["type"] = str(type)

    preset_id = None if preset is None else str(preset).strip().lower()
    if preset_id in ("", "none"):
        preset_id = None
    if preset_id is not None and preset_id not in PRESETS:
        raise ValueError(f"unsupported preset: {preset} (use {', '.join(PRESETS)})")
    given = {"format": format, "channels": channels, "bg_color": bg_color, "bg_image": bg_image,
             "bg_image_url": bg_image_url, "crop": crop, "crop_margin": crop_margin, "scale": scale,
             "position": position}
    if preset_id:
        for name in PRESET_BLOCKED:
            if given[name] is not None and given[name] is not False:
                raise ValueError(f"{name} cannot be used with a preset: the preset decides it")
        for key in (extra or {}):
            if str(key).strip().replace("-", "_") in PRESET_BLOCKED + ("bg_image_file",):
                raise ValueError(f"extra option {key} cannot be used with a preset: the preset decides it")
        fields["preset"] = preset_id
    else:
        if format is not None:
            fields["format"] = str(format).lower()
        if channels is not None:
            fields["channels"] = str(channels).lower()
        if bg_color is not None:
            fields["bg_color"] = str(bg_color).lstrip("#")
        if bg_image_url is not None:
            fields["bg_image_url"] = str(bg_image_url)
        if bg_image is not None:
            files["bg_image_file"] = _read_image(bg_image, "bg_image")
        if crop:
            fields["crop"] = "true"
        if crop_margin is not None:
            fields["crop_margin"] = str(crop_margin)
        if scale is not None:
            fields["scale"] = str(scale)
        if position is not None:
            fields["position"] = str(position)

    shadow_kind = None if shadow is None else str(shadow).strip().lower()
    if shadow_kind not in (None, "cast", "none"):
        raise ValueError(f"unsupported shadow: {shadow} (use cast or none)")
    shadow_opts = {"light_angle": light_angle, "shadow_length": shadow_length, "shadow_strength": shadow_strength}
    named = [k for k, v in shadow_opts.items() if v is not None]
    if named and shadow_kind != "cast":
        raise ValueError(f"{named[0]} needs shadow='cast'")
    if shadow_kind == "cast":
        angle = None if light_angle is None else _int_in(light_angle, -90, 90, "light_angle")
        strength = None if shadow_strength is None else _int_in(shadow_strength, 0, 100, "shadow_strength")
        length = None if shadow_length is None else str(shadow_length).strip().lower()
        if length is not None and length not in SHADOW_LENGTHS:
            raise ValueError("shadow_length must be short, medium or long")
        if preset_id == "zalando":
            warnings.warn("zalando allows no shadow, the shadow options are ignored", LassoCutWarning, stacklevel=3)
        else:
            fields["shadow_type"] = "cast"
            if angle is not None:
                fields["shadow_angle"] = str(angle)
            if length is not None:
                fields["shadow_length"] = length
            if strength is not None:
                fields["shadow_opacity"] = str(strength)

    for key, value in (extra or {}).items():
        fields[str(key)] = _form_value(value)
    return fields, files


# -- helpers --------------------------------------------------------------------------------------------------------

def _int_in(value: Any, lo: int, hi: int, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a whole number from {lo} to {hi}")
    try:
        n = int(str(value).strip())
    except ValueError:
        raise ValueError(f"{name} must be a whole number from {lo} to {hi}") from None
    if not lo <= n <= hi:
        raise ValueError(f"{name} must be a whole number from {lo} to {hi}")
    return n


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _read_image(image: ImageInput, name: str) -> Tuple[str, bytes]:
    if isinstance(image, (bytes, bytearray, memoryview)):
        return "image", bytes(image)
    if isinstance(image, (str, os.PathLike)):
        path = Path(image)
        if not path.is_file():
            raise FileNotFoundError(f"{name}: file not found: {path}")
        return path.name, path.read_bytes()
    if hasattr(image, "read"):
        data = image.read()
        if isinstance(data, str):
            raise TypeError(f"{name}: open the file in binary mode ('rb')")
        filename = os.path.basename(str(getattr(image, "name", "") or "")) or "image"
        return filename, bytes(data)
    raise TypeError(f"{name} must be a path, bytes or a binary file object")


def _multipart(fields: Mapping[str, str], files: Mapping[str, Tuple[str, bytes]]) -> Tuple[bytes, str]:
    boundary = "lassocut-" + uuid.uuid4().hex
    parts: List[bytes] = []
    for key, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{_quote(key)}"\r\n\r\n'.encode()
                     + value.encode("utf-8") + b"\r\n")
    for key, (filename, data) in files.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{_quote(key)}"; '
                     f'filename="{_quote(filename)}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                     + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _quote(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', "%22").replace("\r", "").replace("\n", "")


def _lower(items: Any) -> Dict[str, str]:
    return {k.lower(): v for k, v in items}


def _error_for(status: int, headers: Mapping[str, str], payload: bytes) -> LassoCutError:
    first: Dict[str, Any] = {}
    try:
        errors = json.loads(payload.decode("utf-8")).get("errors") or []
        first = errors[0] if errors and isinstance(errors[0], dict) else {}
    except (ValueError, AttributeError, UnicodeDecodeError):
        pass
    code, title, detail = first.get("code"), first.get("title"), first.get("detail")
    retry_after = None
    try:
        retry_after = int(headers.get("retry-after", "")) if headers.get("retry-after") else None
    except ValueError:
        pass
    if status == 402:
        message = "Not enough credits. Buy a pack at https://www.lassocut.com/account/ or use size='preview'."
    elif status == 403:
        message = "The LassoCut API key was rejected."
    elif status == 429:
        message = f"Rate limit reached, retry in {retry_after or 60} seconds."
    elif title:
        message = title + (f": {detail}" if detail else "")
    elif status >= 500:
        message = "LassoCut servers are busy, please retry in a minute."
    else:
        message = f"LassoCut returned HTTP {status}."
    return LassoCutError(message, status=status, code=code, title=title, detail=detail, retry_after=retry_after)
