"""
tests/test_settings_secret_masking.py
============================================================
P1 — /api/settings plaintext credentials (audit finding: server.py:546-549,
camera password + RFID root/impinj ochiq ko'rinadi).

Qamrab oladi:
  1) GET /api/settings hech qachon plaintext secret qaytarmaydi (data + raw)
  2) Masked qiymatni ("********") qayta saqlash ORIGINAL secretni BUZMAYDI
  3) Yangi (haqiqiy, mask bo'lmagan) qiymat to'g'ri saqlanadi
  4) CSRF tokenisiz POST /api/settings rad etiladi

HAQIQIY config/settings.yaml ga HECH QACHON tegilmaydi — barcha testlar
tmp_settings_yaml fixture orqali backend.config.SETTINGS_PATH ni tmp_path ga
yo'naltiradi.
"""
from __future__ import annotations

import backend.config as config_mod
from backend.logger import ui_handler

from conftest import login, get_csrf_token


def _authed(client, monkeypatch, username="secadmin", password="SecPass123!"):
    monkeypatch.setattr(config_mod.AUTH, "enabled", True)
    monkeypatch.setattr(config_mod.AUTH, "username", username)
    monkeypatch.setattr(config_mod.AUTH, "password", password)
    r = login(client, username, password)
    assert r.status_code == 303
    return client


def test_settings_get_masks_secrets(client, monkeypatch, tmp_settings_yaml):
    _authed(client, monkeypatch)
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()

    data = body["data"]
    assert data["camera"]["password"] == "********"
    assert data["rfid"]["username"] == "********"
    assert data["rfid"]["password"] == "********"
    assert data["auth"]["password"] == "********"

    raw = body["raw"]
    assert "CamSecret123" not in raw
    assert "RfidSecret456" not in raw
    assert "AuthSecret789" not in raw
    assert 'username: "********"' in raw
    assert "********" in raw


def test_masked_value_roundtrip_preserves_original_secret(client, monkeypatch, tmp_settings_yaml):
    _authed(client, monkeypatch)
    csrf = get_csrf_token(client)

    got = client.get("/api/settings").json()
    data = got["data"]
    assert data["camera"]["password"] == "********"

    # Foydalanuvchi boshqa (nomaxfiy) maydonni o'zgartiradi, lekin parol
    # maydonini TEGMAYDI -> frontend uni "********" holicha yuboradi.
    data["server"]["port"] = 8123

    r = client.post("/api/settings", json={"data": data},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Diskdagi HAQIQIY qiymat saqlanib qolgan bo'lishi kerak (mask bilan
    # UST-USTIGA yozilmagan) va port o'zgargan bo'lishi kerak.
    from backend import settings_store
    on_disk = settings_store.read_parsed()
    assert on_disk["camera"]["password"] == "CamSecret123"
    assert on_disk["rfid"]["username"] == "root"
    assert on_disk["rfid"]["password"] == "RfidSecret456"
    assert on_disk["auth"]["password"] == "AuthSecret789"
    assert on_disk["server"]["port"] == 8123


def test_explicit_new_secret_value_is_saved(client, monkeypatch, tmp_settings_yaml):
    _authed(client, monkeypatch)
    csrf = get_csrf_token(client)

    data = client.get("/api/settings").json()["data"]
    data["camera"]["password"] = "BrandNewCamPass"   # mask emas, haqiqiy yangi qiymat

    r = client.post("/api/settings", json={"data": data},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200

    from backend import settings_store
    on_disk = settings_store.read_parsed()
    assert on_disk["camera"]["password"] == "BrandNewCamPass"


def test_raw_yaml_roundtrip_preserves_original_secret(client, monkeypatch, tmp_settings_yaml):
    _authed(client, monkeypatch)
    csrf = get_csrf_token(client)

    raw = client.get("/api/settings").json()["raw"]
    assert "********" in raw
    # foydalanuvchi xom YAML ni o'zgartirmasdan qayta yuboradi (masalan faqat
    # bitta izoh qo'shdi) — mask sentinel saqlanib qoladi.
    modified_raw = raw + "\n# operator izohi\n"

    r = client.post("/api/settings", json={"raw": modified_raw},
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200

    from backend import settings_store
    on_disk = settings_store.read_parsed()
    assert on_disk["camera"]["password"] == "CamSecret123"
    assert on_disk["rfid"]["password"] == "RfidSecret456"


def test_settings_post_without_csrf_is_rejected(client, monkeypatch, tmp_settings_yaml):
    _authed(client, monkeypatch)
    data = client.get("/api/settings").json()["data"]
    r = client.post("/api/settings", json={"data": data})   # X-CSRF-Token header yo'q
    assert r.status_code == 403


def test_settings_defaults_also_masked(client, monkeypatch, tmp_settings_yaml):
    _authed(client, monkeypatch)
    r = client.get("/api/settings/defaults")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["camera"]["password"] == "********"
    assert data["auth"]["password"] == "********"


def test_operator_cannot_write_settings(client, monkeypatch, tmp_settings_yaml):
    monkeypatch.setattr(config_mod.AUTH, "enabled", True)
    monkeypatch.setattr(config_mod.AUTH, "username", "settingsadmin")
    monkeypatch.setattr(config_mod.AUTH, "password", "SettingsAdminPass1")
    monkeypatch.setattr(config_mod.AUTH, "operator_username", "settingsop")
    monkeypatch.setattr(config_mod.AUTH, "operator_password", "SettingsOpPass1")
    response = login(client, "settingsop", "SettingsOpPass1")
    assert response.status_code == 303

    csrf = get_csrf_token(client)
    response = client.post(
        "/api/settings", json={"data": {"server": {"port": 8124}}},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 403
    assert response.json()["ok"] is False


def test_invalid_yaml_never_echoes_submitted_secret(client, monkeypatch, tmp_settings_yaml):
    _authed(client, monkeypatch)
    csrf = get_csrf_token(client)
    submitted_secret = "NeverEchoThisNewSecret-92741"

    response = client.post(
        "/api/settings",
        json={"raw": f'camera:\n  password: "{submitted_secret}"\n  broken: [\n'},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400
    assert submitted_secret not in response.text
    assert submitted_secret not in repr(ui_handler.snapshot())
