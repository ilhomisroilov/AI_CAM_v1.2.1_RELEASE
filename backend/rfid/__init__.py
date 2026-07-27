"""
RFID integratsiya qatlami (Impinj R700) — gateway_python loyihasidan ko'chirilgan.

Tarkibi:
  epc_extract.py  — EPC -> RFID raqami (gateway logikasi: lstrip('0') + 1000-9999)
  tag_cache.py    — oqimdan kelgan teglar keshi (eng yaxshi RSSI, vaqt oynasi)
  r700_client.py  — R700 REST mijoz (inventory start/stop)
  r700_stream.py  — R700 SSE oqim tinglovchi (teg hodisalari -> kesh)
  rfid_reader.py  — yuqori darajali o'qigich (R700 + simulator)
  rfid_service.py — orkestrator (ulanish, qayta ulanish, status, trigger_read)
"""
