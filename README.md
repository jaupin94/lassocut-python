# LassoCut for Python

Official Python client for the [LassoCut API](https://www.lassocut.com/docs/): remove the background of an
image, frame it for a marketplace, add a cast shadow. Python 3.10 or later, no dependencies (standard library only).

```sh
pip install lassocut
```

```python
from lassocut import LassoCut

client = LassoCut()   # reads LASSOCUT_API_KEY; or LassoCut(api_key="...")
client.remove_background_file("photo.jpg", "photo-cutout.png")
```

Get a key in your [LassoCut account](https://www.lassocut.com/account/). Each account gets 50 free previews a month
(up to 10 a day); full-size results use credits.

## Usage

```python
from lassocut import LassoCut, LassoCutError

client = LassoCut()

# A path, bytes or a binary file object; or image_url="https://..."
result = client.remove_background("photo.jpg", size="preview")
result.save("photo-cutout.png")          # result.data holds the bytes

print(result.credits_charged)            # 0.25 for a preview, 1 for full size, 0 for a free preview
print(result.width, result.height)       # size of the returned image
print(result.type, result.foreground)    # subject type, (top, left, width, height)

# A coloured background and a crop to the subject
client.remove_background_file("shoe.jpg", "shoe.jpg", bg_color="ffffff", crop=True, format="jpg")

# Balance
balance = client.credits()
print(balance.credits, balance.free_previews)
```

`remove_background` takes `size` (`preview`, `full`, `50MP` or `auto`, default `auto`), `type`, `format`,
`channels`, `bg_color`, `bg_image` (path, bytes or file), `bg_image_url`, `crop`, `crop_margin`, `scale`,
`position`, `preset`, `shadow`, `light_angle`, `shadow_length`, `shadow_strength`, and `extra={...}` for any other
API field (for example `extra={"roi": "0% 0% 100% 100%"}`). The fields are described in the
[API reference](https://www.lassocut.com/docs/).

`remove_background_file(input_path, output_path=None, **options)` writes the result; without `output_path` it
writes `<name>-lassocut.<ext>` next to the input, the extension following the returned format.

### Marketplace presets and cast shadow

```python
result = client.remove_background("product.jpg", preset="amazon")
result.save("product-amazon.jpg")
print(result.preset, result.compliant, result.compliance_reasons)   # amazon True []

client.remove_background_file("product.jpg", "product-shopify.jpg", preset="shopify",
                              shadow="cast", light_angle=30, shadow_length="long", shadow_strength=55)
```

`preset` is one of `amazon`, `ebay`, `etsy`, `shopify`, `zalando`, `instagram`. A preset decides the format,
background and framing itself, so it cannot be combined with `format`, `channels`, `bg_color`, `bg_image`,
`bg_image_url`, `crop`, `crop_margin`, `scale` or `position` (a `ValueError` is raised before any call).
Results are JPG files. `result.compliant` is `True` when the result meets the preset; compliant means compliant
with the image rules LassoCut checks. `result.upscaled` is `True` when the product had to be enlarged to fill the
canvas.

`shadow="cast"` adds a cast shadow: `light_angle` from -90 to 90 (positive = light from the left, default 45),
`shadow_length` `short`, `medium` or `long` (default `medium`), `shadow_strength` from 0 to 100. Zalando allows no
shadow, so with `preset="zalando"` the shadow options are ignored with a `LassoCutWarning`.

### Errors and retries

Temporary server errors (502, 503, 504) are retried automatically, 3 attempts in all with a short backoff. Every
attempt carries the same `Idempotency-Key`, so a retried call is charged once. Other failures raise
`LassoCutError` with `status`, `code` (for example `unknown_foreground`), `title`, `detail` and, for 429,
`retry_after`:

```python
try:
    client.remove_background("photo.jpg")
except LassoCutError as e:
    print(e.status, e.code, e)
```

Options: `LassoCut(api_key=None, api_url=None, timeout=180, max_attempts=3, backoff=1.0)`. `api_url` defaults to
`LASSOCUT_API_URL`, then `https://api.lassocut.com/v1.0`.

## Migrating from remove.bg

The remove.bg API closes on 1 December 2026. LassoCut's `POST /v1.0/removebg` takes the same request fields and
the same `X-Api-Key` header, and returns the image bytes with the same kind of response headers, so existing code
needs the base URL and the key changed:

```diff
 import requests

 r = requests.post(
-    "https://api.remove.bg/v1.0/removebg",
-    headers={"X-Api-Key": REMOVE_BG_KEY},
+    "https://api.lassocut.com/v1.0/removebg",
+    headers={"X-Api-Key": LASSOCUT_API_KEY},
     files={"image_file": open("photo.jpg", "rb")},
     data={"size": "auto"},
 )
```

Or with this package:

```python
from lassocut import LassoCut
LassoCut().remove_background_file("photo.jpg", "no-bg.png", size="auto")
```

If `LASSOCUT_API_KEY` is not set, the client also reads `REMOVE_BG_API_KEY`, so a script configured for the old
service runs once you put a LassoCut key in that variable. LassoCut is an independent service, not affiliated with
remove.bg. More in the [migration guide](https://www.lassocut.com/migrate/).

## Development

```sh
python -m pytest            # or: python -m unittest discover -s tests
python -m build             # sdist and wheel in dist/
```

The tests run against a local stub server: no network and no key needed.

## License

MIT, Copyright (c) 2026 JAUPIN Design LLC.
