"""Conservative, local text-contrast screening for moving editorial artwork."""

from PIL import Image, ImageColor

TEXT_CONTRAST_TARGET = 4.5
BACKING_COLORS = ((13, 20, 28), (247, 245, 240))


def luminance(rgb):
    channels = [value / 255 for value in rgb]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return sum(
        weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), linear, strict=True)
    )


def contrast_ratio(first, second):
    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _percentile(histogram, fraction):
    target = sum(histogram) * fraction
    count = 0
    for value, frequency in enumerate(histogram):
        count += frequency
        if count >= target:
            return value
    return 255


def local_backing(image: Image.Image, rect, text_color, *, target=TEXT_CONTRAST_TARGET):
    """Return minimum dark backing alpha and estimated worst text contrast.

    The 5th and 95th brightness percentiles limit sensitivity to isolated pixels.
    This is a screening estimate, not a per-glyph WCAG certification.
    """
    histogram = image.crop(rect).convert("L").histogram()
    brightness = (_percentile(histogram, 0.05), _percentile(histogram, 0.95))
    color = ImageColor.getrgb(text_color) if isinstance(text_color, str) else text_color
    choices = []
    for backing in BACKING_COLORS:
        for alpha in (*range(0, 256, 8), 255):
            samples = [
                tuple(
                    round(value * (1 - alpha / 255) + base * alpha / 255)
                    for value, base in zip((level,) * 3, backing, strict=True)
                )
                for level in brightness
            ]
            ratio = min(contrast_ratio(color, sample) for sample in samples)
            if ratio >= target:
                choices.append((alpha, ratio, backing))
                break
    if not choices:
        raise ValueError("text color cannot meet contrast target with a local backing")
    return min(choices, key=lambda choice: choice[0])
