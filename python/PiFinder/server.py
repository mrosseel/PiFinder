import functools
import io
import threading
from contextlib import closing
import tempfile
import zipfile
import json
import logging
import secrets
import time
import os
import argparse
import sys
import multiprocessing
from datetime import timezone

import pydeepskylog as pds
from PIL import Image, ImageDraw
from PiFinder import utils, calc_utils, config
from PiFinder import timez
from PiFinder.db.observations_db import (
    ObservationsDatabase,
)
from PiFinder.equipment import Telescope, Eyepiece
from PiFinder.locations import Location
from PiFinder.keyboard_interface import KeyboardInterface
from PiFinder.multiproclogging import MultiprocLogging

from flask import (
    Flask,
    g,
    abort,
    after_this_request,
    request,
    jsonify,
    send_file,
    send_from_directory,
    redirect,
    session,
    make_response,
)
from urllib.parse import quote, urlparse
from flask_babel import Babel, gettext  # type: ignore[import-untyped]
from werkzeug.routing import IntegerConverter
from waitress import serve as waitress_serve

from PiFinder import i18n  # noqa: F401

# Type annotation for the global _ function installed by gettext.install()
import builtins

_ = builtins._  # type: ignore[attr-defined]


# Custom converter to handle negative integers in Flask routes
class SignedIntConverter(IntegerConverter):
    regex = r"-?\d+"


sys_utils = utils.get_sys_utils()

logger = logging.getLogger("Server")
logs_logger = logging.getLogger("Server.Logs")

# Cache for one day: the vendored JS/CSS only changes with a software update.
STATIC_MAX_AGE = 86400


def load_session_secret() -> str:
    """Return the persistent secret that signs the session cookie.

    The secret lives in the writable data dir so that a PiFinder restart does
    not log every browser out. It is created on first use with mode 0600.
    """
    secret_file = utils.data_dir / "web_secret"
    try:
        secret = secret_file.read_text().strip()
        if len(secret) >= 32:
            return secret
    except OSError:
        pass
    secret = secrets.token_hex(32)
    try:
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.touch(mode=0o600, exist_ok=True)
        secret_file.chmod(0o600)
        secret_file.write_text(secret)
    except OSError as e:
        logger.warning("Could not persist web session secret: %s", e)
    return secret


def placeholder_screen() -> Image.Image:
    """Image shown when no PiFinder screen is available (e.g. standalone server)."""
    img = Image.new("RGB", (128, 128), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 127, 127), outline=(128, 0, 0))
    for i, line in enumerate(("PiFinder", "no screen", "available")):
        draw.text((12, 40 + i * 16), line, fill=(255, 0, 0))
    return img


def safe_redirect_target(url, default="/"):
    """Only allow redirects to a local path; reject other hosts and schemes."""
    if not url:
        return default
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc:
        return default
    if not url.startswith("/") or url.startswith("//"):
        return default
    return url


def parse_coordinate(value, field_name):
    """Parse a coordinate/measurement field, accepting comma or period decimals."""
    if value is None:
        raise ValueError(_("%s is required") % field_name)
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError:
        raise ValueError(_("%s must be a number") % field_name)


def parse_utc_datetime(date_str, time_str):
    """Parse ``YYYY-MM-DD`` + ``H:M[:S]`` (zero padding optional) as UTC."""
    parts = [int(p) for p in time_str.strip().split(":")]
    if len(parts) == 2:
        parts.append(0)
    if len(parts) != 3:
        raise ValueError(_("Time must be HH:MM:SS"))
    hh, mm, ss = parts
    datetime_obj = timez.parse(
        f"{date_str.strip()} {hh:02d}:{mm:02d}:{ss:02d}", "%Y-%m-%d %H:%M:%S"
    )
    return datetime_obj.replace(tzinfo=timezone.utc)


def auth_required(func):
    @functools.wraps(func)
    def auth_wrapper(*args, **kwargs):
        # check for and validate session
        if session.get("authenticated"):
            return func(*args, **kwargs)

        # Pass the original path via ?next= so Safari preserves it across redirects
        next_url = request.full_path.rstrip("?")
        return redirect(f"/login?next={quote(next_url, safe='')}")

    return auth_wrapper


class MockSharedState:
    """Mock shared state for standalone testing"""

    def __init__(self):
        self._location = type(
            "Location", (), {"lock": False, "lat": None, "lon": None, "altitude": None}
        )()
        self._screen_img = None
        self._solve_state = False
        self._solution = None

    def location(self):
        return self._location

    def screen(self):
        return self._screen_img

    def solve_state(self):
        return self._solve_state

    def solution(self):
        return self._solution


def server_locale():
    # Try to get from user preferences, session, or accept languages
    # For now, default to English
    return request.accept_languages.best_match(["en", "fr", "de", "es", "zh"]) or "en"


class Server:
    NETWORK_STATUS_TTL = 5.0
    SCREEN_PNG_TTL = 0.1

    def __init__(
        self,
        keyboard_queue=None,
        ui_queue=None,
        gps_queue=None,
        shared_state=None,
        is_debug=False,
    ):
        self._software_version = utils.get_version()
        self.keyboard_queue = keyboard_queue or multiprocessing.Queue()
        self.ui_queue = ui_queue or multiprocessing.Queue()
        self.gps_queue = gps_queue or multiprocessing.Queue()
        self.shared_state = shared_state or MockSharedState()
        self.ki = KeyboardInterface()
        # gps info
        self.lat = None
        self.lon = None
        self.altitude = None
        self.gps_locked = False

        self.button_dict = {
            "PLUS": self.ki.PLUS,
            "MINUS": self.ki.MINUS,
            "SQUARE": self.ki.SQUARE,
            "LEFT": self.ki.LEFT,
            "UP": self.ki.UP,
            "DOWN": self.ki.DOWN,
            "RIGHT": self.ki.RIGHT,
            "ALT_PLUS": self.ki.ALT_PLUS,
            "ALT_MINUS": self.ki.ALT_MINUS,
            "ALT_LEFT": self.ki.ALT_LEFT,
            "ALT_UP": self.ki.ALT_UP,
            "ALT_DOWN": self.ki.ALT_DOWN,
            "ALT_RIGHT": self.ki.ALT_RIGHT,
            "ALT_0": self.ki.ALT_0,
            "ALT_SQUARE": self.ki.ALT_SQUARE,
            "LNG_LEFT": self.ki.LNG_LEFT,
            "LNG_UP": self.ki.LNG_UP,
            "LNG_DOWN": self.ki.LNG_DOWN,
            "LNG_RIGHT": self.ki.LNG_RIGHT,
            "LNG_SQUARE": self.ki.LNG_SQUARE,
            "POWER_BTN": self.ki.POWER_BTN,
        }

        self.network = sys_utils.Network()
        self._net_status_cache: tuple = (None, 0.0)
        self._net_status_lock = threading.Lock()
        self._screen_cache: tuple = (None, 0.0)
        self._screen_lock = threading.Lock()

        # Initialize Flask app with absolute template path
        views2_path = os.path.join(os.path.dirname(__file__), "..", "views")
        views2_path = os.path.abspath(views2_path)
        logger.debug(f"Template folder path: {views2_path}")

        app = Flask(__name__, template_folder=views2_path)
        app.secret_key = load_session_secret()
        # SameSite=Lax: the browser does not send the cookie on cross-site
        # POSTs, so with every state change behind POST this covers CSRF
        # without a token system.
        app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
        app.config["SESSION_COOKIE_HTTPONLY"] = True
        # Register the custom signed integer converter
        app.url_map.converters["signed_int"] = SignedIntConverter

        logger.info(f"Flask app created successfully: {app}")
        logger.info(f"Template folder: {app.template_folder}")

        # Setup Babel for i18n
        Babel(app, locale_selector=server_locale)  # Picked up by app variable

        # Configure Jinja2 environment for i18n
        app.jinja_env.add_extension("jinja2.ext.i18n")

        # Use PiFinder's global gettext function in templates
        app.jinja_env.globals["_"] = builtins._

        # Static files routes. send_from_directory refuses paths that escape
        # the given directory.
        @app.route("/images/<path:filename>")
        def send_image(filename):
            return send_from_directory(
                os.path.join(views2_path, "images"), filename, max_age=STATIC_MAX_AGE
            )

        @app.route("/js/<path:filename>")
        def send_js(filename):
            return send_from_directory(
                os.path.join(views2_path, "js"), filename, max_age=STATIC_MAX_AGE
            )

        @app.route("/css/<path:filename>")
        def send_css(filename):
            return send_from_directory(
                os.path.join(views2_path, "css"), filename, max_age=STATIC_MAX_AGE
            )

        @app.route("/")
        def home():
            # logger.debug("/ called")
            # Get version info

            software_version = self._software_version

            # Try to update GPS state
            try:
                self.update_gps()
            except Exception as e:
                logger.error(f"Failed to update GPS in home route: {str(e)}")

            # Use GPS data if available
            lat_text = str(self.lat) if self.gps_locked else ""
            lon_text = str(self.lon) if self.gps_locked else ""
            gps_icon = "gps_fixed" if self.gps_locked else "gps_off"
            gps_text = gettext("Locked") if self.gps_locked else gettext("Not Locked")

            # Default camera values
            ra_text = "0"
            dec_text = "0"
            camera_icon = "broken_image"

            # Try to get solution data
            try:
                if self.shared_state.solve_state() is True:
                    camera_icon = "camera_alt"
                    solution = self.shared_state.solution()
                    if solution and solution.has_pointing():
                        aligned = solution.pointing.aligned.estimate
                        hh, mm, _ = calc_utils.ra_to_hms(aligned.RA)
                        ra_text = f"{hh:02.0f}h{mm:02.0f}m"
                        dec_text = f"{aligned.Dec: .2f}"
            except Exception as e:
                logger.error(f"Failed to get solution data: {str(e)}")

            # Render the template with available data
            net = self.network_status()
            return app.jinja_env.get_template("index.html").render(
                title=gettext("Home"),
                software_version=software_version,
                wifi_mode=net["wifi_mode"],
                ip=net["ip"],
                network_name=net["network_name"],
                gps_icon=gps_icon,
                gps_text=gps_text,
                lat_text=lat_text,
                lon_text=lon_text,
                camera_icon=camera_icon,
                ra_text=ra_text,
                dec_text=dec_text,
            )

        @app.route("/login", methods=["GET", "POST"])
        def login():
            if request.method == "POST":
                password = request.form.get("password")
                # Read from hidden form field (set by GET handler); fall back to session
                origin_url = safe_redirect_target(
                    request.form.get("origin_url") or session.get("origin_url")
                )
                if sys_utils.verify_password("pifinder", password):
                    session["authenticated"] = True
                    session.pop("origin_url", None)
                    return redirect(origin_url)
                else:
                    return app.jinja_env.get_template("login.html").render(
                        title=gettext("Login"),
                        origin_url=origin_url,
                        error_message=gettext("Invalid Password"),
                    )
            else:
                # Prefer ?next= URL param (set by auth_required); fall back to session
                origin_url = safe_redirect_target(
                    request.args.get("next") or session.get("origin_url")
                )
                return app.jinja_env.get_template("login.html").render(
                    title=gettext("Login"), origin_url=origin_url
                )

        @app.route("/remote")
        @auth_required
        def remote():
            return app.jinja_env.get_template("remote.html").render(title=_("Remote"))

        @app.route("/advanced")
        @auth_required
        def advanced():
            return app.jinja_env.get_template("advanced.html").render(
                title=_("Advanced")
            )

        @app.route("/network")
        @auth_required
        def network_page():
            show_new_form = request.args.get("add_new", 0)

            return app.jinja_env.get_template("network.html").render(
                title=_("Network"),
                net=self.network,
                show_new_form=show_new_form,
            )

        @app.route("/gps")
        @auth_required
        def gps_page():
            self.update_gps()
            show_new_form = request.args.get("add_new", 0)
            logger.debug(
                "/gps: %f, %f, %f ",
                self.lat or 0.0,
                self.lon or 0.0,
                self.altitude or 0.0,
            )

            return app.jinja_env.get_template("gps.html").render(
                title=_("GPS"),
                show_new_form=show_new_form,
                lat=self.lat,
                lon=self.lon,
                altitude=self.altitude,
            )

        @app.route("/gps/update", methods=["POST"])
        @auth_required
        def gps_update():
            date_req = request.form.get("date")
            time_req = request.form.get("time")
            try:
                lat = parse_coordinate(
                    request.form.get("latitudeDecimal"), _("Latitude")
                )
                lon = parse_coordinate(
                    request.form.get("longitudeDecimal"), _("Longitude")
                )
                altitude = parse_coordinate(request.form.get("altitude"), _("Altitude"))
                if not (-90 <= lat <= 90):
                    raise ValueError(_("Latitude must be between -90 and 90"))
                if not (-180 <= lon <= 180):
                    raise ValueError(_("Longitude must be between -180 and 180"))
                datetime_utc = None
                if time_req and date_req:
                    datetime_utc = parse_utc_datetime(date_req, time_req)
            except ValueError as e:
                self.update_gps()
                return app.jinja_env.get_template("gps.html").render(
                    title=_("GPS"),
                    show_new_form=0,
                    lat=self.lat,
                    lon=self.lon,
                    altitude=self.altitude,
                    error_message=str(e),
                )
            gps_lock(lat, lon, altitude)
            if datetime_utc is not None:
                time_lock(datetime_utc)
            logger.debug(
                "GPS update: %s, %s, %s, %s, %s", lat, lon, altitude, date_req, time_req
            )
            return redirect("/")

        @app.route("/locations")
        @auth_required
        def locations_page():
            show_new_form = request.args.get("add_new", 0)
            cfg = self._cfg()
            return app.jinja_env.get_template("locations.html").render(
                title=_("Locations"),
                locations=cfg.locations.locations,
                show_new_form=show_new_form,
            )

        @app.route("/locations/add", methods=["POST"])
        @auth_required
        def location_add():
            try:
                name = (request.form.get("name") or "").strip()
                lat = parse_coordinate(request.form.get("latitude"), _("Latitude"))
                lon = parse_coordinate(request.form.get("longitude"), _("Longitude"))
                altitude = parse_coordinate(request.form.get("altitude"), _("Altitude"))
                error_in_m = parse_coordinate(
                    request.form.get("error_in_m", "0"), _("Error")
                )
                source = request.form.get("source", "Manual Entry")

                # Server-side validation
                if not name:
                    raise ValueError(_("Location name is required"))
                if not (-90 <= lat <= 90):
                    raise ValueError(_("Latitude must be between -90 and 90"))
                if not (-180 <= lon <= 180):
                    raise ValueError(_("Longitude must be between -180 and 180"))
                if not (-1000 <= altitude <= 10000):
                    raise ValueError(
                        _("Altitude must be between -1000 and 10000 meters")
                    )
                if not (0 <= error_in_m <= 10000):
                    raise ValueError(_("Error must be between 0 and 10000 meters"))

                new_location = Location(
                    name=name,
                    latitude=lat,
                    longitude=lon,
                    height=altitude,
                    error_in_m=error_in_m,
                    source=source,
                )

                cfg = self._cfg()
                cfg.locations.add_location(new_location)
                cfg.save_locations()

                self.ui_queue.put("reload_config")
                return redirect("/locations")

            except ValueError as e:
                return app.jinja_env.get_template("locations.html").render(
                    title=_("Locations"),
                    locations=self._cfg().locations.locations,
                    show_new_form=1,
                    error_message=str(e),
                )

        @app.route("/locations/rename/<int:location_id>", methods=["POST"])
        @auth_required
        def location_rename(location_id):
            try:
                cfg = self._cfg()

                if not (0 <= location_id < len(cfg.locations.locations)):
                    raise ValueError("Invalid location ID")

                name = (request.form.get("name") or "").strip()
                lat = parse_coordinate(request.form.get("latitude"), _("Latitude"))
                lon = parse_coordinate(request.form.get("longitude"), _("Longitude"))
                altitude = parse_coordinate(request.form.get("altitude"), _("Altitude"))
                error_in_m = parse_coordinate(
                    request.form.get("error_in_m", "0"), _("Error")
                )
                source = request.form.get("source", "Manual Entry")

                # Server-side validation
                if not name:
                    raise ValueError(_("Location name is required"))
                if not (-90 <= lat <= 90):
                    raise ValueError(_("Latitude must be between -90 and 90"))
                if not (-180 <= lon <= 180):
                    raise ValueError(_("Longitude must be between -180 and 180"))
                if not (-1000 <= altitude <= 10000):
                    raise ValueError(
                        _("Altitude must be between -1000 and 10000 meters")
                    )
                if not (0 <= error_in_m <= 10000):
                    raise ValueError(_("Error must be between 0 and 10000 meters"))

                location = cfg.locations.locations[location_id]
                location.name = name
                location.latitude = lat
                location.longitude = lon
                location.height = altitude
                location.error_in_m = error_in_m
                location.source = source

                cfg.save_locations()
                self.ui_queue.put("reload_config")
                return redirect("/locations")

            except ValueError as e:
                return app.jinja_env.get_template("locations.html").render(
                    title=_("Locations"),
                    locations=self._cfg().locations.locations,
                    show_new_form=0,
                    error_message=str(e),
                )

        @app.route("/locations/delete/<int:location_id>", methods=["POST"])
        @auth_required
        def location_delete(location_id):
            cfg = self._cfg()
            if 0 <= location_id < len(cfg.locations.locations):
                location = cfg.locations.locations[location_id]
                cfg.locations.remove_location(location)
                cfg.save_locations()
                # Notify main process to reload config
                self.ui_queue.put("reload_config")
            return redirect("/locations")

        @app.route("/locations/set_default/<int:location_id>", methods=["POST"])
        @auth_required
        def location_set_default(location_id):
            cfg = self._cfg()
            if 0 <= location_id < len(cfg.locations.locations):
                location = cfg.locations.locations[location_id]
                cfg.locations.set_default(location)
                cfg.save_locations()
                # Notify main process to reload config
                self.ui_queue.put("reload_config")
            return redirect("/locations")

        @app.route("/locations/load/<int:location_id>", methods=["POST"])
        @auth_required
        def location_load(location_id):
            cfg = self._cfg()
            if 0 <= location_id < len(cfg.locations.locations):
                location = cfg.locations.locations[location_id]
                gps_lock(location.latitude, location.longitude, location.height)
            return redirect("/locations")

        @app.route("/network/add", methods=["POST"])
        @auth_required
        def network_add():
            ssid = (request.form.get("ssid") or "").strip()
            psk = request.form.get("psk") or ""
            if not ssid:
                return app.jinja_env.get_template("network.html").render(
                    title=_("Network"),
                    net=self.network,
                    show_new_form=1,
                    error_message=_("Network name is required"),
                )
            if len(psk) < 8:
                key_mgmt = "NONE"
            else:
                key_mgmt = "WPA-PSK"

            self.network.add_wifi_network(ssid, key_mgmt, psk)
            self.invalidate_network_status()
            return redirect("/network")

        @app.route("/network/delete/<int:network_id>", methods=["POST"])
        @auth_required
        def network_delete(network_id):
            self.network.delete_wifi_network(network_id)
            self.invalidate_network_status()
            return redirect("/network")

        @app.route("/network/update", methods=["POST"])
        @auth_required
        def network_update():
            wifi_mode = request.form.get("wifi_mode")
            ap_name = request.form.get("ap_name")
            host_name = request.form.get("host_name")

            self.network.set_wifi_mode(wifi_mode)
            self.network.set_ap_name(ap_name)
            self.network.set_host_name(host_name)
            self.invalidate_network_status()

            applied_host = self.network.get_host_name()
            return app.jinja_env.get_template("network.html").render(
                title=_("Network"),
                net=self.network,
                show_new_form=0,
                status_message=_(
                    "Network settings updated — no restart needed. This device is "
                    "now reachable at http://{host}.local. If you changed the host "
                    "name, the previous address stops working, so reconnect there."
                ).format(host=applied_host),
            )

        @app.route("/tools/pwchange", methods=["POST"])
        @auth_required
        def password_change():
            current_password = request.form.get("current_password")
            new_passworda = request.form.get("new_passworda")
            new_passwordb = request.form.get("new_passwordb")

            if new_passworda == "" or current_password == "" or new_passwordb == "":
                return app.jinja_env.get_template("tools.html").render(
                    title=_("Tools"),
                    error_message=_("You must fill in all password fields"),
                )

            if new_passworda == new_passwordb:
                if sys_utils.change_password(
                    "pifinder", current_password, new_passworda
                ):
                    return app.jinja_env.get_template("tools.html").render(
                        title=_("Tools"), status_message=_("Password Changed")
                    )
                else:
                    return app.jinja_env.get_template("tools.html").render(
                        title=_("Tools"), error_message=_("Incorrect current password")
                    )
            else:
                return app.jinja_env.get_template("tools.html").render(
                    title=_("Tools"), error_message=_("New passwords do not match")
                )

        @app.route("/system/restart", methods=["POST"])
        @auth_required
        def system_restart():
            """
            Restarts the RPI system
            """
            sys_utils.restart_system()
            return "restarting"

        @app.route("/system/restart_pifinder", methods=["POST"])
        @auth_required
        def pifinder_restart():
            """
            Restarts just the PiFinder software
            """
            sys_utils.restart_pifinder()
            return "restarting"

        @app.route("/equipment")
        @auth_required
        def equipment():
            return app.jinja_env.get_template("equipment.html").render(
                title=_("Equipment"), equipment=self._cfg().equipment
            )

        @app.route(
            "/equipment/set_active_instrument/<int:instrument_id>", methods=["POST"]
        )
        @auth_required
        def set_active_instrument(instrument_id: int):
            cfg = self._cfg()
            if not (0 <= instrument_id < len(cfg.equipment.telescopes)):
                abort(404)
            cfg.equipment.set_active_telescope(cfg.equipment.telescopes[instrument_id])
            cfg.save_equipment()
            self.ui_queue.put("reload_config")
            return app.jinja_env.get_template("equipment.html").render(
                title=_("Equipment"),
                equipment=cfg.equipment,
                success_message=cfg.equipment.active_telescope.make
                + " "
                + cfg.equipment.active_telescope.name
                + " "
                + _("set as active instrument."),
            )

        @app.route("/equipment/set_active_eyepiece/<int:eyepiece_id>", methods=["POST"])
        @auth_required
        def set_active_eyepiece(eyepiece_id: int):
            cfg = self._cfg()
            if not (0 <= eyepiece_id < len(cfg.equipment.eyepieces)):
                abort(404)
            cfg.equipment.set_active_eyepiece(cfg.equipment.eyepieces[eyepiece_id])
            cfg.save_equipment()
            self.ui_queue.put("reload_config")
            return app.jinja_env.get_template("equipment.html").render(
                title=_("Equipment"),
                equipment=cfg.equipment,
                success_message=cfg.equipment.active_eyepiece.make
                + " "
                + cfg.equipment.active_eyepiece.name
                + " "
                + _("set as active eyepiece."),
            )

        @app.route("/equipment/import_from_deepskylog", methods=["POST"])
        @auth_required
        def equipment_import():
            username = request.form.get("dsl_name")
            cfg = self._cfg()
            if username:
                instruments = pds.dsl_instruments(username)
                for instrument in instruments:
                    if instrument["type"] == 0:
                        # Skip the naked eye
                        continue

                    make = instrument["instrument_make"]["name"]

                    obstruction_perc = instrument["obstruction_perc"]
                    if obstruction_perc is None:
                        obstruction_perc = 0
                    else:
                        obstruction_perc = float(obstruction_perc)

                    # Convert the html special characters (ampersand, quote, ...) in instrument["name"]
                    # to the corresponding character
                    instrument["name"] = instrument["name"].replace("&amp;", "&")
                    instrument["name"] = instrument["name"].replace("&quot;", '"')
                    instrument["name"] = instrument["name"].replace("&apos;", "'")
                    instrument["name"] = instrument["name"].replace("&lt;", "<")
                    instrument["name"] = instrument["name"].replace("&gt;", ">")

                    new_instrument = Telescope(
                        make=make,
                        name=instrument["name"],
                        aperture_mm=int(instrument["diameter"]),
                        focal_length_mm=int(instrument["diameter"] * instrument["fd"]),
                        obstruction_perc=obstruction_perc,
                        mount_type=instrument["mount_type"]["name"].lower(),
                        flip_image=bool(instrument["flip_image"]),
                        flop_image=bool(instrument["flop_image"]),
                        reverse_arrow_a=False,
                        reverse_arrow_b=False,
                    )
                    try:
                        cfg.equipment.telescopes.index(new_instrument)
                    except ValueError:
                        cfg.equipment.telescopes.append(new_instrument)

                # Add the eyepieces from deepskylog
                eyepieces = pds.dsl_eyepieces(username)
                for eyepiece in eyepieces:
                    # Convert the html special characters (ampersand, quote, ...) in eyepiece["name"]
                    # to the corresponding character
                    eyepiece["name"] = eyepiece["name"].replace("&amp;", "&")
                    eyepiece["name"] = eyepiece["name"].replace("&quot;", '"')
                    eyepiece["name"] = eyepiece["name"].replace("&apos;", "'")
                    eyepiece["name"] = eyepiece["name"].replace("&lt;", "<")
                    eyepiece["name"] = eyepiece["name"].replace("&gt;", ">")

                    make = eyepiece["eyepiece_make"]["name"]

                    new_eyepiece = Eyepiece(
                        make=make,
                        name=eyepiece["name"],
                        focal_length_mm=float(eyepiece["focalLength"]),
                        afov=int(eyepiece["apparentFOV"]),
                        field_stop=float(eyepiece["field_stop_mm"]),
                    )
                    try:
                        cfg.equipment.eyepieces.index(new_eyepiece)
                    except ValueError:
                        cfg.equipment.add_eyepiece(new_eyepiece)

                cfg.save_equipment()
                self.ui_queue.put("reload_config")
            return app.jinja_env.get_template("equipment.html").render(
                title=_("Equipment"),
                equipment=self._cfg().equipment,
                success_message=_(
                    "Equipment Imported, restart your PiFinder to use this new data"
                ),
            )

        @app.route("/equipment/edit_eyepiece/<signed_int:eyepiece_id>")
        @auth_required
        def edit_eyepiece(eyepiece_id: int):
            if eyepiece_id >= 0:
                eyepieces = self._cfg().equipment.eyepieces
                if eyepiece_id >= len(eyepieces):
                    abort(404)
                eyepiece = eyepieces[eyepiece_id]
            else:
                eyepiece = Eyepiece(
                    make="", name="", focal_length_mm=0, afov=0, field_stop=0
                )

            return app.jinja_env.get_template("edit_eyepiece.html").render(
                title=_("Edit Eyepiece"), eyepiece=eyepiece, eyepiece_id=eyepiece_id
            )

        @app.route("/equipment/add_eyepiece/<signed_int:eyepiece_id>", methods=["POST"])
        @auth_required
        def equipment_add_eyepiece(eyepiece_id: int):
            cfg = self._cfg()

            try:
                make = request.form.get("make") or ""
                name = request.form.get("name") or ""
                focal_length_str = request.form.get("focal_length_mm") or "0"
                afov_str = request.form.get("afov") or "0"
                field_stop_str = request.form.get("field_stop") or "0"

                eyepiece = Eyepiece(
                    make=make,
                    name=name,
                    focal_length_mm=float(focal_length_str),
                    afov=int(afov_str),
                    field_stop=float(field_stop_str),
                )

                if eyepiece_id >= 0:
                    if eyepiece_id >= len(cfg.equipment.eyepieces):
                        abort(404)
                    cfg.equipment.eyepieces[eyepiece_id] = eyepiece
                else:
                    try:
                        index = cfg.equipment.eyepieces.index(eyepiece)
                        cfg.equipment.eyepieces[index] = eyepiece
                    except ValueError:
                        cfg.equipment.eyepieces.append(eyepiece)

                cfg.save_equipment()
                self.ui_queue.put("reload_config")
            except ValueError as e:
                logger.error(f"Error adding eyepiece: {e}")
                return app.jinja_env.get_template("edit_eyepiece.html").render(
                    title=_("Edit Eyepiece"),
                    eyepiece=Eyepiece(
                        make=make,
                        name=name,
                        focal_length_mm=0,
                        afov=0,
                        field_stop=0,
                    ),
                    eyepiece_id=eyepiece_id,
                    error_message=_(
                        "Focal length, apparent FOV and field stop must be numbers"
                    ),
                )

            return app.jinja_env.get_template("equipment.html").render(
                title=_("Equipment"),
                equipment=self._cfg().equipment,
                success_message=_("Eyepiece added, restart your PiFinder to use"),
            )

        @app.route("/equipment/delete_eyepiece/<int:eyepiece_id>", methods=["POST"])
        @auth_required
        def equipment_delete_eyepiece(eyepiece_id: int):
            cfg = self._cfg()
            if not (0 <= eyepiece_id < len(cfg.equipment.eyepieces)):
                abort(404)
            cfg.equipment.eyepieces.pop(eyepiece_id)
            cfg.save_equipment()
            self.ui_queue.put("reload_config")
            return app.jinja_env.get_template("equipment.html").render(
                title=_("Equipment"),
                equipment=self._cfg().equipment,
                success_message=_(
                    "Eyepiece Deleted, restart your PiFinder to remove from menu"
                ),
            )

        @app.route("/equipment/edit_instrument/<signed_int:instrument_id>")
        @auth_required
        def edit_instrument(instrument_id: int):
            if instrument_id >= 0:
                telescopes = self._cfg().equipment.telescopes
                if instrument_id >= len(telescopes):
                    abort(404)
                telescope = telescopes[instrument_id]
            else:
                telescope = Telescope(
                    make="",
                    name="",
                    aperture_mm=0,
                    focal_length_mm=0,
                    obstruction_perc=0,
                    mount_type="",
                    flip_image=False,
                    flop_image=False,
                    reverse_arrow_a=False,
                    reverse_arrow_b=False,
                )

            return app.jinja_env.get_template("edit_instrument.html").render(
                title=_("Edit Instrument"),
                telescope=telescope,
                instrument_id=instrument_id,
            )

        @app.route(
            "/equipment/add_instrument/<signed_int:instrument_id>", methods=["POST"]
        )
        @auth_required
        def equipment_add_instrument(instrument_id: int):
            cfg = self._cfg()

            try:
                make = request.form.get("make") or ""
                name = request.form.get("name") or ""
                aperture_str = request.form.get("aperture") or "0"
                focal_length_str = request.form.get("focal_length_mm") or "0"
                obstruction_str = request.form.get("obstruction_perc") or "0"
                mount_type = request.form.get("mount_type") or ""

                instrument = Telescope(
                    make=make,
                    name=name,
                    aperture_mm=int(aperture_str),
                    focal_length_mm=int(focal_length_str),
                    obstruction_perc=float(obstruction_str),
                    mount_type=mount_type,
                    flip_image=bool(request.form.get("flip")),
                    flop_image=bool(request.form.get("flop")),
                    reverse_arrow_a=bool(request.form.get("reverse_arrow_a")),
                    reverse_arrow_b=bool(request.form.get("reverse_arrow_b")),
                )
                if instrument_id >= 0:
                    if instrument_id >= len(cfg.equipment.telescopes):
                        abort(404)
                    cfg.equipment.telescopes[instrument_id] = instrument
                else:
                    try:
                        index = cfg.equipment.telescopes.index(instrument)
                        cfg.equipment.telescopes[index] = instrument
                    except ValueError:
                        cfg.equipment.telescopes.append(instrument)

                cfg.save_equipment()
                self.ui_queue.put("reload_config")
            except ValueError as e:
                logger.error(f"Error adding instrument: {e}")
                return app.jinja_env.get_template("edit_instrument.html").render(
                    title=_("Edit Instrument"),
                    telescope=Telescope(
                        make=make,
                        name=name,
                        aperture_mm=0,
                        focal_length_mm=0,
                        obstruction_perc=0,
                        mount_type=mount_type,
                        flip_image=False,
                        flop_image=False,
                        reverse_arrow_a=False,
                        reverse_arrow_b=False,
                    ),
                    instrument_id=instrument_id,
                    error_message=_(
                        "Aperture, focal length and obstruction must be numbers"
                    ),
                )
            return app.jinja_env.get_template("equipment.html").render(
                title=_("Equipment"),
                equipment=self._cfg().equipment,
                success_message=_("Instrument Added, restart your PiFinder to use"),
            )

        @app.route("/equipment/delete_instrument/<int:instrument_id>", methods=["POST"])
        @auth_required
        def equipment_delete_instrument(instrument_id: int):
            cfg = self._cfg()
            if not (0 <= instrument_id < len(cfg.equipment.telescopes)):
                abort(404)
            cfg.equipment.telescopes.pop(instrument_id)
            cfg.save_equipment()
            self.ui_queue.put("reload_config")
            return app.jinja_env.get_template("equipment.html").render(
                title=_("Equipment"),
                equipment=self._cfg().equipment,
                success_message=_(
                    "Instrument Deleted, restart your PiFinder to remove from menu"
                ),
            )

        @app.route("/observations")
        @auth_required
        def obs_sessions():
            with closing(ObservationsDatabase()) as obs_db:
                return self._obs_sessions(app, obs_db)

        @app.route("/observations/<session_id>")
        @auth_required
        def obs_session(session_id):
            with closing(ObservationsDatabase()) as obs_db:
                return self._obs_session(app, obs_db, session_id)

        @app.route("/tools")
        @auth_required
        def tools():
            return app.jinja_env.get_template("tools.html").render(title=_("Tools"))

        @app.route("/logs")
        @auth_required
        def logs_page():
            return app.jinja_env.get_template("logs.html").render(title=_("Logs"))

        @app.route("/logs/stream")
        @auth_required
        def stream_logs():
            TAIL_BYTES = 100 * 1024  # serve only the last 100 KB on first load
            t0 = time.monotonic()
            try:
                position = int(request.args.get("position", 0))
                log_file = os.path.expanduser("~/PiFinder_data/pifinder.log")

                try:
                    file_size = os.path.getsize(log_file)
                    logs_logger.debug(
                        "stream_logs: position=%d file_size=%d", position, file_size
                    )

                    # Reset when file shrank (rotation) or on first call; tail large files.
                    if position > file_size or position == 0:
                        position = max(0, file_size - TAIL_BYTES)

                    t1 = time.monotonic()
                    with open(log_file, "r") as f:
                        f.seek(position)
                        new_lines = f.readlines()
                        new_position = f.tell()
                    logs_logger.debug(
                        "stream_logs: read %d lines (%d bytes) in %.3fs",
                        len(new_lines),
                        new_position - position,
                        time.monotonic() - t1,
                    )

                    if new_position - position > 1024 * 1024:
                        logs_logger.warning(
                            "stream_logs: large response %.1f MB",
                            (new_position - position) / 1e6,
                        )

                    if new_lines:
                        return jsonify({"logs": new_lines, "position": new_position})
                    else:
                        return jsonify({"logs": [], "position": new_position})
                except FileNotFoundError:
                    logger.error(f"Log file not found: {log_file}")
                    return jsonify({"logs": [], "position": 0, "file_not_found": True})

            except Exception as e:
                logger.error(f"Error streaming logs: {e}")
                return jsonify({"logs": [], "position": position})
            finally:
                logger.debug("stream_logs: total %.3fs", time.monotonic() - t0)

        @app.route("/logs/download")
        @auth_required
        def download_logs():
            try:
                # Create a temporary zip file
                timestamp = timez.local_now().strftime("%Y%m%d_%H%M%S")

                with tempfile.NamedTemporaryFile(
                    delete=False, suffix=".zip"
                ) as temp_file:
                    zip_path = temp_file.name

                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                    # Add all log files
                    log_dir = os.path.expanduser("~/PiFinder_data")
                    for filename in os.listdir(log_dir):
                        if filename.startswith("pifinder") and filename.endswith(
                            ".log"
                        ):
                            file_path = os.path.join(log_dir, filename)
                            zipf.write(file_path, filename)

                @after_this_request
                def remove_file(response):
                    try:
                        os.remove(zip_path)
                    except OSError:
                        pass
                    return response

                return send_file(
                    zip_path,
                    as_attachment=True,
                    download_name=f"logs_{timestamp}.zip",
                    mimetype="application/zip",
                )

            except Exception as e:
                logger.error(f"Error creating log zip: {e}")
                return app.jinja_env.get_template("logs.html").render(
                    title=_("Logs"), error_message=_("Error creating log archive")
                )

        @app.route("/logs/configs")
        @auth_required
        def list_log_configs():
            """Return all available logconf_*.json presets with display names."""
            active = utils.active_logconf_name()
            configs = []
            for name in utils.available_logconfs():
                stem = name[len("logconf_") : -len(".json")]
                configs.append(
                    {
                        "file": name,
                        "name": stem.replace("_", " ").title(),
                        "active": name == active,
                    }
                )
            return jsonify({"configs": configs})

        @app.route("/logs/switch_config", methods=["POST"])
        @auth_required
        def switch_log_config():
            """Persist the chosen log config to the data dir, then restart."""
            logconf_file = request.form.get("logconf_file", "").strip()
            try:
                utils.set_active_logconf(logconf_file)
                logger.info("Switched log config to %s", logconf_file)
            except (ValueError, FileNotFoundError):
                return jsonify(
                    {"status": "error", "message": "Invalid log config file name"}
                )
            except Exception as e:
                logger.error("Failed to switch log config: %s", e)
                return jsonify({"status": "error", "message": str(e)})
            return app.jinja_env.get_template("restart_pifinder.html").render(
                title=_("Restarting PiFinder")
            )

        @app.route("/logs/upload_config", methods=["POST"])
        @auth_required
        def upload_log_config():
            """Upload a new logconf_*.json file."""
            upload = request.files.get("config_file")
            if not upload:
                logger.warning("No file provided for log config upload")
                return jsonify({"status": "error", "message": "No file provided"})
            filename = os.path.basename(upload.filename or "")
            if not utils.is_logconf_filename(filename):
                logger.warning("Invalid log config file name: %s", upload.filename)
                return jsonify(
                    {
                        "status": "error",
                        "message": "File must be named logconf_<name>.json",
                    }
                )
            target = utils.user_logconf_dir / filename
            if target.exists() or (utils.logconf_dir / filename).exists():
                logger.warning("Log config file already exists: %s", filename)
                return jsonify(
                    {
                        "status": "error",
                        "message": f"File already exists: {filename}",
                    }
                )
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                upload.save(str(target))
                logger.info("Uploaded log config: %s", target)
                return jsonify({"status": "ok", "file": filename})
            except Exception as e:
                logger.error("Failed to save uploaded log config: %s", e)
                return jsonify({"status": "error", "message": "Could not save file"})

        @app.route("/tools/backup")
        @auth_required
        def tools_backup():
            _backup_file = sys_utils.backup_userdata()

            # Assumes the standard backup location
            return send_file(
                os.path.expanduser("~/PiFinder_data/PiFinder_backup.zip"),
                as_attachment=True,
            )

        @app.route("/tools/restore", methods=["POST"])
        @auth_required
        def tools_restore():
            sys_utils.remove_backup()
            backup_file = request.files.get("backup_file")
            if backup_file:
                backup_file.save(
                    os.path.expanduser("~/PiFinder_data/PiFinder_backup.zip")
                )

                sys_utils.restore_userdata(
                    os.path.expanduser("~/PiFinder_data/PiFinder_backup.zip")
                )

            return app.jinja_env.get_template("restart_pifinder.html").render(
                title=_("Restart PiFinder")
            )

        @app.route("/key_callback", methods=["POST"])
        @auth_required
        def key_callback():
            button = request.json.get("button")
            if button in self.button_dict:
                self.key_callback(self.button_dict[button])
            else:
                self.key_callback(int(button))
            return jsonify({"message": "success"})

        @app.route("/api/current-selection")
        @auth_required
        def current_selection():
            """
            Returns information about the currently active UI item for testing purposes
            """
            try:
                ui_state_data = self.shared_state.current_ui_state()
                if ui_state_data is None:
                    return jsonify({"error": "UI state not available"})

                return jsonify(ui_state_data)

            except Exception as e:
                logger.error(f"Error getting current UI state: {e}")
                return jsonify({"error": str(e)})

        @app.route("/image")
        def serve_pil_image():
            response = make_response(self.screen_png())
            response.headers["Content-Type"] = "image/png"
            response.headers["Cache-Control"] = "no-store"
            return response

        try:
            from PiFinder.api_extensions import register_api_routes

            register_api_routes(app, self, require_auth=True)
        except Exception:
            logger.exception("Failed to register API extension routes")

        def gps_lock(lat: float = 50, lon: float = 3, altitude: float = 10):
            msg = (
                "fix",
                {
                    "lat": lat,
                    "lon": lon,
                    "altitude": altitude,
                    "error_in_m": 0,
                    "source": "WEB",
                    "lock": True,
                },
            )
            self.gps_queue.put(msg)
            logger.debug("Putting location msg on gps_queue: {msg}")

        def time_lock(time=None):
            msg = ("time", time or timez.local_now())
            self.gps_queue.put(msg)
            logger.debug("Putting time msg on gps_queue: {msg}")

        # Store the app reference for running
        self.app = app

    def _cfg(self):
        """One Config per request: the JSON files are read once per request."""
        if "pifinder_config" not in g:
            g.pifinder_config = config.Config()
        return g.pifinder_config

    def network_status(self) -> dict:
        """Wifi mode, IP and active network label, cached for a few seconds.

        Each of these asks NetworkManager for its connection list, which is
        slow on the Pi and was done several times per home-page load.
        """
        now = time.monotonic()
        with self._net_status_lock:
            cached, stamp = self._net_status_cache
            if cached is not None and now - stamp < self.NETWORK_STATUS_TTL:
                return cached
            status = {
                "wifi_mode": self.network.wifi_mode(),
                "ip": self.network.local_ip(),
                "network_name": self.network.get_active_label(),
            }
            self._net_status_cache = (status, now)
            return status

    def invalidate_network_status(self):
        with self._net_status_lock:
            self._net_status_cache = (None, 0.0)

    def screen_png(self) -> bytes:
        """PNG bytes of the current screen, re-encoded at most every 100 ms.

        The remote page polls at ~10 Hz; without this every poll pays for a
        shared-state round trip and a PNG encode.
        """
        now = time.monotonic()
        with self._screen_lock:
            png, stamp = self._screen_cache
            if png is not None and now - stamp < self.SCREEN_PNG_TTL:
                return png
            img = None
            try:
                img = self.shared_state.screen()
            except (BrokenPipeError, EOFError):
                pass
            if img is None:
                img = placeholder_screen()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png = buf.getvalue()
            self._screen_cache = (png, now)
            return png

    def _obs_sessions(self, app, obs_db):
        if request.args.get("download", 0) == "1":
            # Download all as TSV
            observations = obs_db.observations_as_tsv()

            response = make_response(observations)
            response.headers["Content-Disposition"] = (
                "attachment; filename=observations.tsv"
            )
            response.headers["Content-Type"] = "text/tab-separated-values"
            return response

        # regular html page of sessions
        sessions = obs_db.get_sessions()
        metadata = {
            "sess_count": len(sessions),
            "object_count": sum(x["observations"] for x in sessions),
            "total_duration": sum(x["duration"] for x in sessions),
        }
        return app.jinja_env.get_template("obs_sessions.html").render(
            title=_("Observations"), sessions=sessions, metadata=metadata
        )

    def _obs_session(self, app, obs_db, session_id):
        if request.args.get("download", 0) == "1":
            # Download all as TSV
            observations = obs_db.observations_as_tsv(session_id)

            response = make_response(observations)
            response.headers["Content-Disposition"] = (
                f"attachment; filename=observations_{session_id}.tsv"
            )
            response.headers["Content-Type"] = "text/tab-separated-values"
            return response

        sessions = obs_db.get_sessions(session_id)
        if not sessions:
            abort(404)
        session = sessions[0]
        objects = obs_db.get_logs_by_session(session_id)
        ret_objects = []
        for obj in objects:
            obj_ = dict(obj)
            try:
                obj_notes = json.loads(obj_["notes"] or "{}")
            except (TypeError, ValueError):
                obj_notes = {"notes": obj_["notes"]}
            obj_["notes"] = [f"{key}: {value}" for key, value in obj_notes.items()]
            ret_objects.append(obj_)
        return app.jinja_env.get_template("obs_session_log.html").render(
            title=_("Session Log"), session=session, objects=ret_objects
        )

    def run(self):
        # If the PiFinder software is running as a service
        # it can grab port 80.  If not, it needs to use 8080
        try:
            waitress_serve(self.app, host="0.0.0.0", port=80)
            logger.info("Webserver started on port 80")
        except (PermissionError, OSError) as e:
            logger.debug(f"Permission denied on port 80, trying 8080. {e}")
            try:
                waitress_serve(self.app, host="0.0.0.0", port=8080)
                logger.info("Webserver started on port 8080")
            except Exception as e2:
                logger.exception(f"Failed to start server on port 8080. {e2}")
                raise
        logger.debug("Webserver is running")

    def key_callback(self, key):
        self.keyboard_queue.put(key)

    def update_gps(self):
        """Update GPS information"""
        location = self.shared_state.location()

        if location.lock is True:
            self.gps_locked = True
            self.lat = location.lat
            self.lon = location.lon
            self.altitude = location.altitude
        else:
            self.gps_locked = False
            self.lat = None
            self.lon = None
            self.altitude = None


def run_server(
    keyboard_queue, ui_queue, gps_queue, shared_state, log_queue, verbose=False
):
    MultiprocLogging.configurer(log_queue)
    server = Server(keyboard_queue, ui_queue, gps_queue, shared_state, verbose)
    server.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="PiFinder Flask Web Server with i18n support"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--port", type=int, default=8080, help="Port to run server on (default: 8080)"
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)"
    )

    args = parser.parse_args()

    # Setup basic logging for standalone mode
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s:%(levelname)s:%(message)s",
    )

    logger.info("Starting PiFinder Server in standalone mode")

    # Create a single queue for command line testing
    test_queue: multiprocessing.Queue = multiprocessing.Queue()

    # Create server with mock components
    server = Server(
        keyboard_queue=test_queue,
        ui_queue=test_queue,
        gps_queue=test_queue,
        shared_state=MockSharedState(),
        is_debug=args.debug,
    )

    # Override the default port behavior for command line usage
    try:
        logger.info("Starting web server.")
        server.run()
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server failed to start: {e}")
        sys.exit(1)
