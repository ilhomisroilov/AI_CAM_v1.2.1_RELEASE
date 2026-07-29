# AI_CAM Documentation and Presentation Source Attribution

## Repository sources

The package was built from the current release tree. Principal sources:

- `README.md`, `VERSION`, `CHANGELOG.md`, `RELEASE_NOTES_v1.2.1.md`
- `config/settings.yaml`, `backend/config.py`
- `run.py`, `backend/server.py`, `backend/pipeline.py`
- `backend/camera/camera_client.py`
- `backend/plc/plc_service.py`, `backend/plc/plc_melsec.py`, `backend/plc/edge_audit.py`
- `backend/rfid/`
- `backend/ai/detector.py`, `ocr_worker.py`, `vin_fusion.py`, `vin_rules.py`, `vin_postprocess.py`
- `backend/ai/ocr_shadow_hook.py`, `ocr_shadow_stage.py`, `ocr_orchestrator.py`, `engines/`
- `backend/ai/ocr_collector.py`, `dataset_collector.py`
- `backend/database/db.py`, `ocr_v121_db.py`, `migrations/v1_2_1_ocr_evidence.sql`
- `frontend/templates/`, `frontend/static/`
- current tests and existing documentation/release reports

## Official external sources

- SICK, Lector64x/Lector65x operating instructions:  
  https://cdn.sick.com/media/docs/3/53/453/operating_instructions_lector64x_65x_flex_lector65x_dynamic_focus_fr_im0071453.pdf
- SICK, Lector63x/Lector64x/Lector65x online help:  
  https://www.sick.com/media/docs/4/34/534/online_help_lector63x_lector64x_lector65x_en_im0077534.pdf
- Mitsubishi Electric, MELSEC Communication Protocol Reference Manual:  
  https://www.mitsubishielectric.com/dl/fa/document/manual/plc/sh080008/sh080008ab.pdf
- Impinj, R700 Series RAIN RFID Readers datasheet:  
  https://support.impinj.com/hc/article_attachments/31243539924371
- Impinj, IoT Device Interface overview:  
  https://support.impinj.com/article/32123172324755
- Impinj, HTTP/HTTPS data stream configuration:  
  https://support.impinj.com/article/360017447560
- Impinj, transient inventory API example:  
  https://support.impinj.com/article/32170781585427

## Generated visual

`reports/assets/ai_cam_isometric_hero.png` was generated with OpenAI ImageGen for this package.

Prompt summary: premium 16:9 semi-3D isometric automotive inspection cell; unbranded vehicle body, industrial camera, PLC cabinet, four-antenna RFID zone, edge computer, luminous AI/OCR/data paths; dark graphite/midnight blue, cyan highlights, small amber trigger accent; negative space for title; no logos, readable text, watermark, fake software UI, or excessive sci-fi clutter.

## Asset boundary

The release tree contains no runtime screenshots, VIN crops, production database, or other raster project evidence beyond placeholders. The package therefore:

- does not invent fake production screenshots;
- labels the operator screen and VIN example as schematics;
- keeps installed-device variants and live performance claims unresolved where the repository lacks proof.

