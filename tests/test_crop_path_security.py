"""
tests/test_crop_path_security.py
============================================================
P2 — /crops/{name} path traversal (audit finding: server.py:587-592, faqat
.exists() tekshiriladi -> Windows'da "..\\" bilan CROPS_DIR tashqarisiga
chiqish mumkin).

HAQIQIY data/crops papkasiga HECH QACHON tegilmaydi — backend.server.CROPS_DIR
har bir testda tmp_path ga monkeypatch qilinadi.
"""
from __future__ import annotations

from urllib.parse import quote

import backend.server as server_mod
import backend.config as config_mod

from conftest import login


def _authed(client, monkeypatch, username="cropuser", password="CropPass123!"):
    monkeypatch.setattr(config_mod.AUTH, "enabled", True)
    monkeypatch.setattr(config_mod.AUTH, "username", username)
    monkeypatch.setattr(config_mod.AUTH, "password", password)
    r = login(client, username, password)
    assert r.status_code == 303
    return client


def _make_crop_root(tmp_path, monkeypatch):
    crops_root = tmp_path / "crops_root"
    crops_root.mkdir()
    (crops_root / "valid.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("SHOULD NEVER BE SERVED")
    monkeypatch.setattr(server_mod, "CROPS_DIR", crops_root)
    return crops_root, outside


def test_valid_file_inside_crop_root_is_served(client, monkeypatch, tmp_path):
    _authed(client, monkeypatch)
    _make_crop_root(tmp_path, monkeypatch)
    r = client.get("/crops/valid.jpg")
    assert r.status_code == 200


def test_dotdot_slash_traversal_rejected(client, monkeypatch, tmp_path):
    _authed(client, monkeypatch)
    _make_crop_root(tmp_path, monkeypatch)
    r = client.get("/crops/" + quote("../outside_secret.txt", safe=""))
    assert r.status_code in (400, 404)
    assert b"SHOULD NEVER BE SERVED" not in r.content


def test_windows_backslash_traversal_rejected(client, monkeypatch, tmp_path):
    _authed(client, monkeypatch)
    _make_crop_root(tmp_path, monkeypatch)
    r = client.get("/crops/" + quote("..\\outside_secret.txt", safe=""))
    assert r.status_code in (400, 404)
    assert b"SHOULD NEVER BE SERVED" not in r.content


def test_absolute_path_rejected(client, monkeypatch, tmp_path):
    _authed(client, monkeypatch)
    crops_root, outside = _make_crop_root(tmp_path, monkeypatch)
    r = client.get("/crops/" + quote(str(outside), safe=""))
    assert r.status_code in (400, 404)
    assert b"SHOULD NEVER BE SERVED" not in r.content


def test_disallowed_extension_rejected(client, monkeypatch, tmp_path):
    _authed(client, monkeypatch)
    crops_root, _ = _make_crop_root(tmp_path, monkeypatch)
    (crops_root / "script.py").write_text("print('no')")
    r = client.get("/crops/script.py")
    assert r.status_code == 400


def test_nonexistent_file_returns_404(client, monkeypatch, tmp_path):
    _authed(client, monkeypatch)
    _make_crop_root(tmp_path, monkeypatch)
    r = client.get("/crops/does_not_exist.jpg")
    assert r.status_code == 404
