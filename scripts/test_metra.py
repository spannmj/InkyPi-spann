"""Renders the Metra plugin at every supported resolution/orientation.

Usage:  python scripts/test_metra.py [output.png]
"""

import os
import sys
from unittest.mock import MagicMock

from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from plugins.plugin_registry import load_plugins, get_plugin_instance  # noqa: E402
from utils.image_utils import change_orientation, resize_image  # noqa: E402

RESOLUTIONS = [
    [400, 300],   # Inky wHAT
    [640, 400],   # Inky Impression 4"
    [600, 448],   # Inky Impression 5.7"
    [800, 480],   # Inky Impression 7.3"
]
ORIENTATIONS = ["horizontal", "vertical"]

PLUGIN_CONFIG = {"id": "metra", "class": "Metra", "display_name": "Metra"}
PLUGIN_SETTINGS = {
    "metraLine": "UP-NW",
    "originStop": "EDISONPK",
    "destinationStop": "OTC",
    "walkMinutes": "10",
    "departureCount": "4",
    "useRealtime": "true",
    "showAlerts": "true",
}

load_plugins([PLUGIN_CONFIG])
plugin = get_plugin_instance(PLUGIN_CONFIG)

device_config = MagicMock()
device_config.load_env_key.return_value = os.getenv("METRA_API_KEY")


def config_value(key, default=None):
    if key == "timezone":
        return "America/Chicago"
    if key == "orientation":
        return config_value.orientation
    return default


device_config.get_config.side_effect = config_value

out_dir = sys.argv[1] if len(sys.argv) > 1 else "mock_display_output/metra"
os.makedirs(out_dir, exist_ok=True)

# Chrome on macOS clamps headless windows to a 500px minimum width, which clips
# narrow renders. The board scales proportionally with the viewport, so render
# small sizes upscaled and downsample for an accurate preview.
MIN_VIEWPORT_WIDTH = 500


def render(resolution, orientation):
    render_width = resolution[1] if orientation == "vertical" else resolution[0]
    scale = 1
    while render_width * scale < MIN_VIEWPORT_WIDTH:
        scale += 1

    device_config.get_resolution.return_value = [resolution[0] * scale, resolution[1] * scale]
    config_value.orientation = orientation

    image = plugin.generate_image(PLUGIN_SETTINGS, device_config)
    if scale > 1:
        image = image.resize((image.width // scale, image.height // scale), Image.LANCZOS)
    return image


for resolution in RESOLUTIONS:
    for orientation in ORIENTATIONS:
        image = render(resolution, orientation)
        image = change_orientation(image, orientation)
        image = resize_image(image, resolution, [])
        if orientation == "vertical":
            # Rotate back so the preview reads the way it hangs on the wall.
            image = image.rotate(-90, expand=1)

        path = os.path.join(out_dir, f"{resolution[0]}x{resolution[1]}_{orientation}.png")
        image.save(path)
        print(f"saved {path}")
