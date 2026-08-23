"""Route-level tests for the web server security and crash fixes.

They drive the real Flask app through the test client with config, the
system utilities and the shared state mocked out, following the pattern of
``test_server_dsl_import.py``.
"""

import io
import zipfile

import pytest

from PiFinder import server as server_module
from PiFinder import sys_utils_base, utils
from PiFinder.equipment import Equipment, Eyepiece, Telescope
from PiFinder.locations import Location, Locations


class FakeConfig:
    """Stands in for config.Config() so no real config file is touched."""

    def __init__(self):
        self.equipment = Equipment(
            telescopes=[
                Telescope(
                    make="Make",
                    name="Scope",
                    aperture_mm=100,
                    focal_length_mm=500,
                    obstruction_perc=0,
                    mount_type="alt-az",
                    flip_image=False,
                    flop_image=False,
                    reverse_arrow_a=False,
                    reverse_arrow_b=False,
                )
            ],
            eyepieces=[
                Eyepiece(
                    make="Make", name="EP", focal_length_mm=10, afov=60, field_stop=0
                )
            ],
        )
        self.locations = Locations(
            locations=[
                Location(
                    name="Home",
                    latitude=50.0,
                    longitude=3.0,
                    height=10.0,
                    error_in_m=0,
                    source="Manual Entry",
                )
            ]
        )
        self.saved_equipment = 0
        self.saved_locations = 0

    def save_equipment(self):
        self.saved_equipment += 1

    def save_locations(self):
        self.saved_locations += 1


@pytest.fixture
def cfg(monkeypatch):
    cfg = FakeConfig()
    monkeypatch.setattr(server_module.config, "Config", lambda: cfg)
    return cfg


@pytest.fixture
def server(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "data_dir", tmp_path)
    srv = server_module.Server()
    srv.app.testing = True
    return srv


@pytest.fixture
def client(server):
    client = server.app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client


@pytest.fixture
def anon_client(server):
    return server.app.test_client()


# --- auth / redirects -------------------------------------------------------


@pytest.mark.unit
def test_auth_required_redirects_to_login_with_next(anon_client):
    response = anon_client.get("/remote")
    assert response.status_code == 302
    assert response.headers["Location"] == "/login?next=%2Fremote"


@pytest.mark.unit
@pytest.mark.parametrize(
    "target",
    ["http://evil.example/", "//evil.example/x", "javascript:alert(1)"],
)
def test_login_rejects_offsite_redirect(anon_client, target):
    response = anon_client.get(f"/login?next={target}")
    assert response.status_code == 200
    assert b'name="origin_url" value="/"' in response.data


@pytest.mark.unit
def test_login_keeps_local_redirect(anon_client):
    response = anon_client.get("/login?next=/equipment")
    assert b'name="origin_url" value="/equipment"' in response.data


@pytest.mark.unit
def test_safe_redirect_target():
    assert server_module.safe_redirect_target(None) == "/"
    assert server_module.safe_redirect_target("/logs") == "/logs"
    assert server_module.safe_redirect_target("https://a/b") == "/"
    assert server_module.safe_redirect_target("//a/b") == "/"
    assert server_module.safe_redirect_target("logs") == "/"


@pytest.mark.unit
def test_session_cookie_is_samesite_lax(server):
    assert server.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert server.app.config["SESSION_COOKIE_HTTPONLY"] is True


# --- static files -----------------------------------------------------------


@pytest.mark.unit
def test_static_file_served_with_cache_header(client):
    response = client.get("/css/style.css")
    assert response.status_code == 200
    assert "max-age=86400" in response.headers["Cache-Control"]


@pytest.mark.unit
def test_static_route_blocks_path_traversal(client):
    response = client.get("/css/../../PiFinder/server.py")
    assert response.status_code == 404


# --- API auth ---------------------------------------------------------------


@pytest.mark.unit
def test_api_mutating_routes_need_login(anon_client):
    assert anon_client.post("/api/key", json={"button": "UP"}).status_code == 401
    assert anon_client.post("/api/stop", json={}).status_code == 401


@pytest.mark.unit
def test_api_key_accepts_known_buttons_only(client, server):
    assert client.post("/api/key", json={"button": "UP"}).status_code == 200
    assert server.keyboard_queue.get(timeout=1) == server.ki.UP
    assert client.post("/api/key", json={"button": 3}).status_code == 200
    assert server.keyboard_queue.get(timeout=1) == 3
    assert client.post("/api/key", json={"button": 12345}).status_code == 400
    assert client.post("/api/key", json={"button": "nope"}).status_code == 400


@pytest.mark.unit
def test_api_read_routes_stay_open(anon_client):
    # /api/time only needs shared state, which the mock provides partially;
    # the point is that it is not a 401.
    assert anon_client.get("/api/time").status_code != 401


# --- mutating routes are POST only -----------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "/locations/delete/0",
        "/locations/set_default/0",
        "/locations/load/0",
        "/network/delete/0",
        "/equipment/delete_instrument/0",
        "/equipment/delete_eyepiece/0",
        "/equipment/set_active_instrument/0",
        "/equipment/set_active_eyepiece/0",
        "/system/restart",
        "/system/restart_pifinder",
    ],
)
def test_state_changes_reject_get(client, path):
    assert client.get(path).status_code == 405


@pytest.mark.unit
def test_delete_location_via_post(client, cfg):
    response = client.post("/locations/delete/0")
    assert response.status_code == 302
    assert cfg.locations.locations == []
    assert cfg.saved_locations == 1


# --- form crash fixes -------------------------------------------------------


@pytest.mark.unit
def test_gps_update_bad_input_renders_error(client, server):
    response = client.post("/gps/update", data={"latitudeDecimal": "abc"})
    assert response.status_code == 200
    assert b"must be a number" in response.data
    assert server.gps_queue.empty()


@pytest.mark.unit
def test_gps_update_accepts_comma_and_unpadded_time(client, server):
    response = client.post(
        "/gps/update",
        data={
            "latitudeDecimal": "50,5",
            "longitudeDecimal": "3",
            "altitude": "10",
            "date": "2026-08-23",
            "time": "9:5:3",
        },
    )
    assert response.status_code == 302
    kind, fix = server.gps_queue.get(timeout=1)
    assert kind == "fix" and fix["lat"] == 50.5
    kind, when = server.gps_queue.get(timeout=1)
    assert kind == "time" and (when.hour, when.minute, when.second) == (9, 5, 3)


@pytest.mark.unit
def test_network_add_without_psk_does_not_crash(client, server, monkeypatch):
    added = []
    monkeypatch.setattr(server.network, "add_wifi_network", lambda *a: added.append(a))
    response = client.post("/network/add", data={"ssid": "MyWifi"})
    assert response.status_code == 302
    assert added == [("MyWifi", "NONE", "")]


@pytest.mark.unit
def test_network_add_requires_ssid(client):
    response = client.post("/network/add", data={"ssid": " "})
    assert response.status_code == 200
    assert b"required" in response.data


@pytest.mark.unit
def test_add_new_eyepiece_dedupes_against_eyepieces(client, cfg):
    existing = cfg.equipment.eyepieces[0]
    response = client.post(
        "/equipment/add_eyepiece/-1",
        data={
            "make": existing.make,
            "name": existing.name,
            "focal_length_mm": str(existing.focal_length_mm),
            "afov": str(existing.afov),
            "field_stop": str(existing.field_stop),
        },
    )
    assert response.status_code == 200
    assert len(cfg.equipment.eyepieces) == 1
    assert cfg.saved_equipment == 1


@pytest.mark.unit
def test_add_eyepiece_bad_number_shows_error(client, cfg):
    response = client.post(
        "/equipment/add_eyepiece/-1",
        data={"make": "X", "name": "Y", "focal_length_mm": "ten"},
    )
    assert response.status_code == 200
    assert b"must be numbers" in response.data
    assert len(cfg.equipment.eyepieces) == 1
    assert cfg.saved_equipment == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "/equipment/delete_eyepiece/5",
        "/equipment/delete_instrument/5",
        "/equipment/set_active_eyepiece/5",
        "/equipment/set_active_instrument/5",
    ],
)
def test_equipment_out_of_range_is_404(client, path):
    assert client.post(path).status_code == 404


@pytest.mark.unit
def test_edit_equipment_out_of_range_is_404(client):
    assert client.get("/equipment/edit_eyepiece/5").status_code == 404
    assert client.get("/equipment/edit_instrument/5").status_code == 404


# --- log config upload ------------------------------------------------------


@pytest.mark.unit
def test_upload_log_config_saves_to_user_dir(client, tmp_path, monkeypatch):
    user_dir = tmp_path / "logconf"
    monkeypatch.setattr(utils, "user_logconf_dir", user_dir)
    monkeypatch.setattr(utils, "logconf_dir", tmp_path / "builtin")
    response = client.post(
        "/logs/upload_config",
        data={"config_file": (io.BytesIO(b"{}"), "logconf_mine.json")},
        content_type="multipart/form-data",
    )
    assert response.get_json()["status"] == "ok"
    assert (user_dir / "logconf_mine.json").read_bytes() == b"{}"
    assert "logconf_mine.json" in utils.available_logconfs()


@pytest.mark.unit
@pytest.mark.parametrize(
    "name", ["evil.json", "logconf_.json", "logconf_a/../../x.json"]
)
def test_upload_log_config_rejects_bad_names(client, tmp_path, monkeypatch, name):
    monkeypatch.setattr(utils, "user_logconf_dir", tmp_path / "logconf")
    response = client.post(
        "/logs/upload_config",
        data={"config_file": (io.BytesIO(b"{}"), name)},
        content_type="multipart/form-data",
    )
    assert response.get_json()["status"] == "error"
    assert not (tmp_path / "logconf").exists()


# --- backup restore ---------------------------------------------------------


def _zip_with(tmp_path, members):
    zip_path = tmp_path / "backup.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return str(zip_path)


@pytest.mark.unit
def test_restore_extracts_inside_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "PiFinder_data"
    data_dir.mkdir()
    monkeypatch.setattr(sys_utils_base.utils, "data_dir", data_dir)
    member = str(data_dir / "config.json").lstrip("/")
    sys_utils_base.restore_userdata(_zip_with(tmp_path, {member: "{}"}))
    assert (data_dir / "config.json").read_text() == "{}"


@pytest.mark.unit
def test_restore_rejects_members_outside_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "PiFinder_data"
    data_dir.mkdir()
    monkeypatch.setattr(sys_utils_base.utils, "data_dir", data_dir)
    outside = str(tmp_path / "pwned.txt").lstrip("/")
    with pytest.raises(ValueError):
        sys_utils_base.restore_userdata(_zip_with(tmp_path, {outside: "x"}))
    assert not (tmp_path / "pwned.txt").exists()


# --- observations notes -----------------------------------------------------


@pytest.mark.unit
def test_session_notes_are_escaped(client, monkeypatch):
    class FakeObsDb:
        def __init__(self):
            pass

        def get_sessions(self, session_uid=None):
            return [{"UID": "s1", "observations": 1, "duration": 1.0}]

        def get_logs_by_session(self, session_uid):
            return [
                {
                    "obs_time_local": "t",
                    "catalog": "M",
                    "sequence": 1,
                    "notes": '{"Seeing": "<script>alert(1)</script>"}',
                }
            ]

        def close(self):
            pass

    monkeypatch.setattr(server_module, "ObservationsDatabase", FakeObsDb)
    response = client.get("/observations/s1")
    assert response.status_code == 200
    assert b"<script>alert(1)</script>" not in response.data
    assert b"&lt;script&gt;" in response.data
