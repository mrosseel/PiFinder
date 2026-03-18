#!/usr/bin/python
# -*- coding:utf-8 -*-
"""
Shared image utility functions for object images

Provides common operations for:
- POSS survey images
- Generated Gaia star charts
"""

import math

from PIL import Image, ImageDraw, ImageChops


def cardinal_vectors(image_rotate, fx=1, fy=1):
    """Return (nx, ny), (ex, ey) unit vectors for North and East.

    image_rotate: degrees the image was rotated (180 + roll for reflectors).
    fx, fy: -1 to mirror that axis (flip/flop), +1 otherwise.
    """
    theta = math.radians(image_rotate)
    n = (fx * math.sin(theta), fy * -math.cos(theta))
    e = (-fx * math.cos(theta), -fy * math.sin(theta))
    return n, e


def size_overlay_points(extents, pa, image_rotate, px_per_arcsec, cx, cy, fx=1, fy=1):
    """Compute outline points for the size overlay.

    Returns a list of (x, y) tuples.
    For 1 extent returns None (caller should use native ellipse).
    """
    if not extents or len(extents) == 1:
        return None

    # Negate image_rotate to match PIL.rotate()'s CCW convention.
    # Without negation, the bug is hidden when roll=0 (±180 ≡ ∓180).
    theta = math.radians(-image_rotate - pa - 90)
    cos_t = math.cos(theta)
    sin_t = math.sin(theta)

    # fx is set by flip_image (FLIP_TOP_BOTTOM = Y negate),
    # fy is set by flop_image (FLIP_LEFT_RIGHT = X negate)
    points = []
    if len(extents) == 2:
        rx = extents[0] * px_per_arcsec / 2
        ry = extents[1] * px_per_arcsec / 2
        for i in range(36):
            t = 2 * math.pi * i / 36
            x = rx * math.cos(t)
            y = ry * math.sin(t)
            points.append(
                (cx + fy * (x * cos_t - y * sin_t), cy + fx * (x * sin_t + y * cos_t))
            )
    else:
        step = 2 * math.pi / len(extents)
        for i, ext in enumerate(extents):
            angle = i * step - math.pi / 2
            r = ext * px_per_arcsec / 2
            x = r * math.cos(angle)
            y = r * math.sin(angle)
            points.append(
                (cx + fy * (x * cos_t - y * sin_t), cy + fx * (x * sin_t + y * cos_t))
            )
    return points


def vertex_overlay_points(
    vertices, obj_ra, obj_dec, image_rotate, px_per_arcsec, cx, cy, fx=1, fy=1
):
    """Project RA/Dec vertex pairs to pixel coords.

    Uses the same linear tangent-plane projection as the Gaia chart renderer
    so that overlay lines align with plotted stars.

    vertices: list of [ra, dec] pairs in degrees.
    obj_ra, obj_dec: object center in degrees.
    Returns list of (x, y) pixel tuples.
    """
    ra0 = math.radians(obj_ra)
    dec0 = math.radians(obj_dec)
    cos_dec0 = math.cos(dec0)

    # pixel_scale equivalent: px_per_arcsec * 3600 * (180/pi) = px_per_radian
    px_per_rad = px_per_arcsec * 206264.806

    # Negate rotation to match Gaia chart convention (PIL rotate CCW)
    rot_rad = math.radians(-image_rotate)
    cos_r = math.cos(rot_rad)
    sin_r = math.sin(rot_rad)

    points = []
    for ra_deg, dec_deg in vertices:
        ra = math.radians(ra_deg)
        dec = math.radians(dec_deg)

        dra = ra - ra0
        # Handle RA wrapping
        if dra > math.pi:
            dra -= 2 * math.pi
        elif dra < -math.pi:
            dra += 2 * math.pi
        ddec = dec - dec0

        # Linear tangent plane (matches Gaia chart render_chart)
        x_proj = dra * cos_dec0
        y_proj = ddec

        # Screen coords: RA increases left (subtract x), Dec increases up (subtract y)
        x_screen = cx - x_proj * px_per_rad
        y_screen = cy - y_proj * px_per_rad

        # Rotate around center
        x_rel = x_screen - cx
        y_rel = y_screen - cy
        x_rot = x_rel * cos_r - y_rel * sin_r
        y_rot = x_rel * sin_r + y_rel * cos_r

        # Apply flip/flop: fx is set by flip_image (FLIP_TOP_BOTTOM = Y negate),
        # fy is set by flop_image (FLIP_LEFT_RIGHT = X negate)
        points.append((cx + fy * x_rot, cy + fx * y_rot))
    return points


def draw_nsew_labels(draw, display_class, image_rotate, cx, cy, fx=1, fy=1):
    """Draw two cardinal labels as a pair (e.g. N+E) in the freest corner.

    Picks the adjacent pair (NE, NW, SE, SW) whose labels are farthest
    from occupied overlay zones (top-left: FOV, top-right: mag, bottom-left: eyepiece).
    Labels are placed at exact circle-edge positions (no clamping) to preserve 90° angles.
    """
    from PiFinder.ui import ui_utils

    (nx, ny), (ex, ey) = cardinal_vectors(image_rotate, fx, fy)
    label_font = display_class.fonts.base
    label_color = display_class.colors.get(64)
    r_label = display_class.fov_res / 2 - 2
    fw = label_font.width
    fh = label_font.height
    resX = display_class.resX
    resY = display_class.resY

    all_labels = {
        "N": (nx, ny),
        "S": (-nx, -ny),
        "E": (ex, ey),
        "W": (-ex, -ey),
    }

    # Overlay rectangles: (x, y, w, h) matching add_image_overlays positions
    tb = display_class.titlebar_height - 1
    obstructions = [
        (1, tb, fw * 5, fh),  # top-left: FOV "0.52°"
        (resX - fw * 4, tb, fw * 4, fh),  # top-right: mag "33x"
        (1, resY - fh * 1.1, fw * 9, fh),  # bottom-left: eyepiece "38mm name"
    ]

    def label_pos(dx, dy):
        lx = cx + dx * r_label - fw / 2
        ly = cy + dy * r_label - fh / 2
        # Pull inward along the radial direction if label goes off-screen
        if lx < 0 or ly < 0 or lx + fw > resX or ly + fh > resY:
            # Shrink radius until label fits, preserving angle
            for shrink in range(1, int(r_label)):
                r2 = r_label - shrink
                lx = cx + dx * r2 - fw / 2
                ly = cy + dy * r2 - fh / 2
                if 0 <= lx and lx + fw <= resX and 0 <= ly and ly + fh <= resY:
                    break
        return lx, ly

    def min_distance_to_obstructions(lx, ly):
        """Minimum distance from label center to nearest obstruction rect."""
        best = float("inf")
        for ox, oy, ow, oh in obstructions:
            # Closest point on rect to (lx, ly)
            clamp_x = max(ox, min(lx, ox + ow))
            clamp_y = max(oy, min(ly, oy + oh))
            d = math.sqrt((lx - clamp_x) ** 2 + (ly - clamp_y) ** 2)
            best = min(best, d)
        return best

    # DEBUG: show all 4 labels to verify angles
    for label in ("N", "S", "E", "W"):
        dx, dy = all_labels[label]
        lx, ly = label_pos(dx, dy)
        ui_utils.shadow_outline_text(
            draw,
            (lx, ly),
            label,
            font=label_font,
            align="left",
            fill=label_color,
            shadow_color=display_class.colors.get(0),
            outline=1,
        )


def _draw_size_overlay_raw(
    draw,
    catalog_object,
    fov,
    image_rotate,
    px_per_arcsec,
    cx,
    cy,
    fx,
    fy,
    overlay_color,
):
    """Low-level shape drawing (no AA). Called at native or supersampled scale."""
    extents = catalog_object.size.extents

    if catalog_object.size.is_segments:
        for segment in extents:
            points = vertex_overlay_points(
                segment,
                catalog_object.ra,
                catalog_object.dec,
                image_rotate,
                px_per_arcsec,
                cx,
                cy,
                fx,
                fy,
            )
            if len(points) == 2:
                draw.line(points, fill=overlay_color, width=1)
    elif catalog_object.size.is_vertices:
        points = vertex_overlay_points(
            extents,
            catalog_object.ra,
            catalog_object.dec,
            image_rotate,
            px_per_arcsec,
            cx,
            cy,
            fx,
            fy,
        )
        if len(points) >= 2:
            draw.line(points, fill=overlay_color, width=1)
    elif len(extents) == 1:
        r = extents[0] * px_per_arcsec / 2
        draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            outline=overlay_color,
            width=1,
        )
    else:
        points = size_overlay_points(
            extents,
            catalog_object.size.position_angle,
            image_rotate,
            px_per_arcsec,
            cx,
            cy,
            fx,
            fy,
        )
        if points:
            draw.polygon(points, outline=overlay_color)


def draw_size_overlay(
    draw,
    catalog_object,
    display_class,
    fov,
    image_rotate,
    cx,
    cy,
    fx=1,
    fy=1,
    color_intensity=None,
    overlay_color=None,
):
    """Draw the object size overlay with 2x supersampled anti-aliasing.

    overlay_color: direct color tuple (RGB or RGBA), bypasses color_intensity lookup.
    color_intensity: override brightness (0-255).  None uses default (100).
    """
    extents = catalog_object.size.extents
    if not extents or fov <= 0:
        return

    px_per_arcsec = display_class.fov_res / (fov * 3600)
    if overlay_color is None:
        overlay_color = display_class.colors.get(
            color_intensity if color_intensity is not None else 100
        )

    # Supersample at 2x for anti-aliasing
    ss = 2
    target_image = draw._image
    w, h = target_image.size
    tmp = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    tmp_draw = ImageDraw.Draw(tmp)

    # Ensure overlay color has alpha for compositing
    if len(overlay_color) == 3:
        ss_color = overlay_color + (255,)
    else:
        ss_color = overlay_color

    _draw_size_overlay_raw(
        tmp_draw,
        catalog_object,
        fov,
        image_rotate,
        px_per_arcsec * ss,
        cx * ss,
        cy * ss,
        fx,
        fy,
        ss_color,
    )

    # Downscale with LANCZOS and composite
    tmp = tmp.resize((w, h), Image.LANCZOS)
    target_image.paste(
        Image.alpha_composite(
            target_image.convert("RGBA"),
            tmp,
        ).convert(target_image.mode),
    )


def add_image_overlays(
    image,
    display_class,
    fov,
    magnification,
    eyepiece,
    burn_in=True,
    limiting_magnitude=None,
):
    """
    Add FOV/magnification/eyepiece overlays to image

    This function is shared by:
    - POSS image display (poss_provider.py)
    - Generated Gaia star charts (chart_provider.py)

    Args:
        image: PIL Image to modify
        display_class: Display configuration object
        fov: Field of view in degrees
        magnification: Telescope magnification
        eyepiece: Active eyepiece object
        burn_in: Whether to add overlays (default True)
        limiting_magnitude: Optional limiting magnitude to display (for generated charts)

    Returns:
        Modified PIL Image with overlays added
    """
    if not burn_in:
        return image

    from PiFinder.ui import ui_utils

    draw = ImageDraw.Draw(image)

    # Top-left: FOV in degrees
    ui_utils.shadow_outline_text(
        draw,
        (1, display_class.titlebar_height - 1),
        f"{fov:0.2f}°",
        font=display_class.fonts.base,
        align="left",
        fill=display_class.colors.get(254),
        shadow_color=display_class.colors.get(0),
        outline=2,
    )

    # Top-right: Magnification
    mag_text = f"{magnification:.0f}x" if magnification and magnification > 0 else "?x"
    ui_utils.shadow_outline_text(
        draw,
        (
            display_class.resX - (display_class.fonts.base.width * 4),
            display_class.titlebar_height - 1,
        ),
        mag_text,
        font=display_class.fonts.base,
        align="right",
        fill=display_class.colors.get(254),
        shadow_color=display_class.colors.get(0),
        outline=2,
    )

    # Top-center: Limiting magnitude (for generated charts)
    if limiting_magnitude is not None:
        # Show ">17" if exceeds catalog limit, otherwise show actual value
        if limiting_magnitude > 17.0:
            lm_text = "LM:>17"
        else:
            lm_text = f"LM:{limiting_magnitude:.1f}"
        lm_bbox = draw.textbbox((0, 0), lm_text, font=display_class.fonts.base.font)
        lm_width = lm_bbox[2] - lm_bbox[0]
        lm_x = (display_class.resX - lm_width) // 2

        ui_utils.shadow_outline_text(
            draw,
            (lm_x, display_class.titlebar_height - 1),
            lm_text,
            font=display_class.fonts.base,
            align="left",
            fill=display_class.colors.get(254),
            shadow_color=display_class.colors.get(0),
            outline=2,
        )

    # Bottom-left: Eyepiece name
    if eyepiece:
        eyepiece_text = f"{eyepiece.focal_length_mm:.0f}mm {eyepiece.name}"
        ui_utils.shadow_outline_text(
            draw,
            (1, display_class.resY - (display_class.fonts.base.height * 1.1)),
            eyepiece_text,
            font=display_class.fonts.base,
            align="left",
            fill=display_class.colors.get(128),  # Dimmer than FOV/mag
            shadow_color=display_class.colors.get(0),
            outline=2,
        )

    return image


def create_loading_image(
    display_class, message="Loading...", progress_text=None, progress_percent=0
):
    """
    Create a placeholder image with loading message and optional progress

    Args:
        display_class: Display configuration object
        message: Main text to display (default "Loading...")
        progress_text: Optional progress status text
        progress_percent: Progress percentage (0-100)

    Returns:
        PIL Image with centered message and progress
    """
    image = Image.new("RGB", display_class.resolution, (0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Use center of display for positioning
    center_x = display_class.resolution[0] // 2
    center_y = display_class.resolution[1] // 2

    # Draw main message
    text_bbox = draw.textbbox((0, 0), message, font=display_class.fonts.large.font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]

    x = center_x - (text_width // 2)
    y = center_y - (text_height // 2) - 20

    draw.text(
        (x, y),
        message,
        font=display_class.fonts.large.font,
        fill=(128, 0, 0),  # Medium red for night vision
    )

    # Draw progress text if provided
    if progress_text:
        progress_bbox = draw.textbbox(
            (0, 0), progress_text, font=display_class.fonts.base.font
        )
        progress_width = progress_bbox[2] - progress_bbox[0]

        px = center_x - (progress_width // 2)
        py = y + text_height + 8

        draw.text(
            (px, py),
            progress_text,
            font=display_class.fonts.base.font,
            fill=(100, 0, 0),  # Dimmer red
        )

    # Draw progress bar if percentage > 0
    if progress_percent > 0:
        bar_width = int(display_class.resolution[0] * 0.8)
        bar_height = 4
        bar_x = center_x - (bar_width // 2)
        bar_y = display_class.resolution[1] - 25

        # Background bar
        draw.rectangle(
            [bar_x, bar_y, bar_x + bar_width, bar_y + bar_height],
            outline=(64, 0, 0),
            fill=(32, 0, 0),
        )

        # Progress fill
        fill_width = int(bar_width * (progress_percent / 100))
        if fill_width > 0:
            draw.rectangle(
                [bar_x, bar_y, bar_x + fill_width, bar_y + bar_height], fill=(128, 0, 0)
            )

        # Percentage text
        percent_text = f"{progress_percent}%"
        percent_bbox = draw.textbbox(
            (0, 0), percent_text, font=display_class.fonts.base.font
        )
        percent_width = percent_bbox[2] - percent_bbox[0]

        draw.text(
            (center_x - (percent_width // 2), bar_y + bar_height + 4),
            percent_text,
            font=display_class.fonts.base.font,
            fill=(100, 0, 0),
        )

    return image


def create_no_image_placeholder(display_class, burn_in=True):
    """
    Create a "No Image" placeholder

    Used when neither POSS nor Gaia chart is available

    Args:
        display_class: Display configuration object
        burn_in: Whether to add text (default True)

    Returns:
        PIL Image with "No Image" message
    """
    image = Image.new("RGB", display_class.resolution)
    if burn_in:
        draw = ImageDraw.Draw(image)
        draw.text(
            (30, 50),
            "No Image",
            font=display_class.fonts.large.font,
            fill=display_class.colors.get(128),
        )
    return image


def apply_circular_vignette(image, display_class):
    """
    Apply circular vignette to show eyepiece FOV boundary

    Creates a circular mask that dims everything outside
    the eyepiece field of view, then adds a subtle outline.

    Args:
        image: PIL Image to modify
        display_class: Display configuration object

    Returns:
        Modified PIL Image with circular vignette
    """
    # Create dimming mask (circle is full brightness, outside is dimmed)
    _circle_dim = Image.new(
        "RGB",
        (display_class.fov_res, display_class.fov_res),
        display_class.colors.get(127),  # Dim the outside
    )
    _circle_draw = ImageDraw.Draw(_circle_dim)
    _circle_draw.ellipse(
        [2, 2, display_class.fov_res - 2, display_class.fov_res - 2],
        fill=display_class.colors.get(255),  # Full brightness inside
    )

    # Apply dimming by multiplying
    image = ImageChops.multiply(image, _circle_dim)

    # Add subtle outline
    draw = ImageDraw.Draw(image)
    draw.ellipse(
        [2, 2, display_class.fov_res - 2, display_class.fov_res - 2],
        outline=display_class.colors.get(64),
        width=1,
    )

    return image


def pad_to_display_resolution(image, display_class):
    """
    Pad image to match display resolution

    If FOV resolution differs from display resolution,
    centers the image and pads with black.

    Args:
        image: PIL Image to pad
        display_class: Display configuration object

    Returns:
        Padded PIL Image at display resolution
    """
    # Pad horizontally if needed
    if display_class.fov_res != display_class.resX:
        pad_image = Image.new("RGB", display_class.resolution)
        pad_image.paste(
            image,
            (
                int((display_class.resX - display_class.fov_res) / 2),
                0,
            ),
        )
        image = pad_image

    # Pad vertically if needed
    if display_class.fov_res != display_class.resY:
        pad_image = Image.new("RGB", display_class.resolution)
        pad_image.paste(
            image,
            (
                0,
                int((display_class.resY - display_class.fov_res) / 2),
            ),
        )
        image = pad_image

    return image
