# AI_CAM v1.1.3 runtime deployment

Bu yo‘riqnoma ikki qo‘llab-quvvatlanadigan profil uchun:

- Windows workstation: CPU fallback, NVIDIA bo‘lmasa ham crash qilmaydi;
- Ubuntu 26.x production server: NVIDIA GPU, YOLO va PaddleOCR ikkalasi CUDA’da.

Runtime OS nomiga qarab CUDA versiyasini taxmin qilmaydi. `nvidia-smi`, Python
ABI, o‘rnatilgan wheel build va real tensor amali tekshiriladi. Ubuntu minor
release yoki NVIDIA driver yangilanganda source kodni o‘zgartirish shart emas.

## Muhim cheklov

Joriy OCR adapter PaddleOCR 2.10.0 API bilan ishlaydi. Sinovdan o‘tgan engine
baseline Python 3.11 va Torch 2.4.1 / TorchVision 0.19.1. CPU profilda
PaddlePaddle 2.6.2 ishlatiladi. Linux GPU profilda esa ataylab Paddle GPU 2.6.1
CUDA 11.8/cuDNN 8.6 `cudnn-in` wheel ishlatiladi; GPU versiyasi CPU pinidan
avtomatik olinmaydi.
Ubuntu’ning default Python versiyasi mos kelmasa, system Python’ni almashtirmang;
AI_CAM uchun alohida Python 3.11 virtual environment yarating.

Paddle’ning hozirgi rasmiy sahifasi 3.x/cu126+ buildlarni tavsiya qilishi mumkin,
lekin ular AI_CAM’ning 2.x OCR adapteri bilan hali regression-testdan o‘tmagan.
Shuning uchun current/latest paketni ko‘r-ko‘rona o‘rnatmang: loyiha pinini faqat
OCR API va production crop regression testi o‘tgandan keyin yangilang. NVIDIA
Ubuntu 26.04’ni rasmiy CUDA Linux guide’da qo‘llaydi, ammo driver borligi Python
framework CUDA wheel’i ishlashini o‘zi kafolatlamaydi.

## Windows — xavfsiz CPU profil

PowerShell’da loyiha katalogidan:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python tools\install_runtime.py --profile cpu
python run.py --self-check --deep
python run.py
```

`requirements.txt` ham aynan shu xavfsiz CPU profilidir. Kompyuterda NVIDIA
ko‘rinsa-yu CPU wheel o‘rnatilgan bo‘lsa, self-check `cpu-fallback` deb ko‘rsatadi;
bu Windows profilida normal. Startup policy OCR’ni CPU’da ishga tushiradi va
GPU so‘ralgani uchun Paddle init’ni yiqilishiga yo‘l qo‘ymaydi.

## Ubuntu 26.x + NVIDIA — production GPU profil

### 1. Host va driverni tekshirish

```bash
nvidia-smi
```

Bu komanda GPU, driver va driver qo‘llaydigan maksimal CUDA versiyasini
ko‘rsatishi kerak. Avval NVIDIA driverni Ubuntu tomonidan tavsiya etilgan usulda
o‘rnating. Python wheel tanlashdan oldin hostga tasodifiy CUDA toolkit yoki cuDNN
aralashtirmang.

### 2. Izolyatsiyalangan Python 3.11 muhit

Python 3.11’ni tashkilotning tasdiqlangan usuli (managed image, `uv`, `pyenv` yoki
ichki package repository) bilan o‘rnating, so‘ng:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

### 3. CUDA runtime o‘rnatish

```bash
python tools/install_runtime.py --profile gpu
```

Installer quyidagilarni bajaradi:

1. Python ABI va virtual environment’ni tekshiradi;
2. `nvidia-smi` orqali driver CUDA imkoniyatini tekshiradi;
3. CPU/GPU Paddle paketlarini bir vaqtda qolishiga yo‘l qo‘ymaydi;
4. Torch `cu118` va Paddle 2.6.1 CUDA 11.8/cuDNN 8.6 `cudnn-in` wheel’ini
   alohida, mos native stack bilan o‘rnatadi;
5. mos install tensor probe’dan o‘tsa qayta yuklab/o‘rnatmaydi;
6. `pip check`, `paddle.utils.run_check()`, real Torch/Paddle tensor self-check
   (Paddle elementwise + cuBLAS matmul + cuDNN conv2d) va production process
   orqali PaddleOCR preload smoke-test’ni bajaradi;
7. GPU serverda barcha gate’lar o‘tmasa non-zero exit bilan to‘xtaydi.

Torch 2.4.1 CUDA wheel CUDA kutubxonalarini virtual muhitdagi
`site-packages/nvidia/*/lib` kataloglariga o‘rnatadi. Paddle `cudnn-in` wheel
cuDNN 8’ni o‘zi bilan olib keladi, lekin `libcublas.so.11` kabi CUDA 11
bog‘liqliklari uchun Torch kataloglari ELF loader yo‘lida bo‘lishi kerak.
Installer va `run.py` bu non-cuDNN kataloglarni child-process muhiti uchun
avtomatik topib ulaydi. Torch tarkibidagi `nvidia/cudnn/lib` ataylab
qo‘shilmaydi: u cuDNN 9 bo‘lib, Paddle 2.6.1 cuDNN 8 ABI bilan aralashtirilmaydi.
NVIDIA pip wheellar faqat versionlangan `libnvrtc.so.11.x`, `libcublas.so.11`
va o‘xshash kutubxonalarni bersa, installer venv ichidagi
`ai_cam_native_libs/` katalogida Paddle so‘raydigan versiyasiz compatibility
symlinklarni (`libnvrtc.so`, `libcublas.so` va boshqalar) yaratadi. O‘rnatilgan
NVIDIA paket fayllari o‘zgartirilmaydi.

Default Torch kanali CUDA 11.8. Paddle artifact esa rasmiy Paddle 2.6 hujjati
ko‘rsatgan `cudnn-in` ro‘yxatidagi Python ABI’ga mos aniq wheel hisoblanadi:

```text
https://www.paddlepaddle.org.cn/whl/linux/cudnnin/stable.html
```

Python 3.11 uchun installer shu ro‘yxat bog‘lagan
`paddlepaddle_gpu-2.6.1-cp311-cp311-linux_x86_64.whl` artifact’ini oladi. Direct
URL ishlamasa fallback faqat shu `cudnn-in` ro‘yxatida va faqat `==2.6.1` uchun
qidiradi; umumiy current Paddle index’iga o‘tmaydi.

Tasdiqlangan mirror kerak bo‘lsa exact wheel URL’ni almashtirish mumkin:

```bash
export AI_CAM_TORCH_INDEX_URL='https://download.pytorch.org/whl/<vendor-channel>'
export AI_CAM_PADDLE_WHEEL_URL='https://approved-mirror/.../paddlepaddle_gpu-2.6.1-cp311-cp311-linux_x86_64.whl'
python tools/install_runtime.py --profile gpu
```

Paddle override HTTPS va exact version/Python ABI/Linux x86_64 filename’iga mos
bo‘lmasa installer fail-closed qiladi. Installer URL’dagi CUDA versiyasini
`nvidia-smi` ko‘rsatgan driver capability bilan solishtiradi. Rasmiy manbalar:

- <https://pytorch.org/get-started/locally/>
- <https://docs.pytorch.org/docs/stable/generated/torch.cuda.is_available.html>
- <https://www.paddlepaddle.org.cn/install/quick>
- <https://www.paddlepaddle.org.cn/documentation/docs/en/2.6/install/pip/linux-pip_en.html>
- <https://www.paddleocr.ai/main/en/version2.x/ppocr/blog/whl.html>
- <https://docs.nvidia.com/cuda/cuda-installation-guide-linux/>

### 4. Majburiy preflight

```bash
python run.py --self-check --deep --require-gpu
```

Exit code `0`, `gpu_pipeline_ready: true`, Torch/Paddle `device_visible: true`,
`compute_ready: true` va `probe_ok: true` bo‘lmasa production liniyani ishga
tushirmang. `--require-gpu` o‘zi ham deep probe’ni majburiy qiladi.

`libcudnn_ops_infer.so.8` yuklanayotganda `libcublas.so.11: cannot open` chiqsa,
checkout eski native-library bootstrap bilan ishlayapti. Yangi kodni olib,
installer’ni o‘sha mavjud venv ichida qayta ishga tushiring; 1.4 GB Paddle
wheel tensor probe o‘tganda qayta yuklanmaydi:

```bash
source .venv-v113/bin/activate
python tools/install_runtime.py --profile gpu
python run.py --self-check --deep --require-gpu
```

### 5. Service gate

Systemd unit’da serverdan oldin shu gate’ni ishlating (user/pathlarni hostga
moslang):

```ini
[Unit]
Description=AI_CAM v1.1.3
After=network-online.target

[Service]
Type=simple
User=ai-cam
WorkingDirectory=/opt/ai-cam
ExecStartPre=/opt/ai-cam/.venv/bin/python run.py --self-check --deep --require-gpu
ExecStart=/opt/ai-cam/.venv/bin/python run.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=AI_CAM_REQUIRE_GPU=1

[Install]
WantedBy=multi-user.target
```

`ExecStartPre` GPU build yoki driver buzilgan deploymentni liniyaga chiqarmaydi.
Server startup’ining o‘zi ham deep tensor gate va PaddleOCR preload’ni tekshiradi.
Linux/NVIDIA hostda `ocr.use_gpu=true` bo‘lsa GPU default fail-closed; systemd’da
`AI_CAM_REQUIRE_GPU=1` talabni explicit qiladi.

Diagnostika uchun CPU fallback ataylab kerak bo‘lsa:

```bash
export AI_CAM_ALLOW_CPU_FALLBACK=1
python run.py
```

Bu production line rejimi emas: `/health` `degraded` qaytaradi va monitor alarm
berishi kerak. Windows NVIDIA’siz CPU profil normal fallback sifatida qoladi.

## Monitoring

Server ishga tushgach:

```bash
curl -fsS http://127.0.0.1:8080/health
```

`components.runtime` ichida:

- `effective_profile`: `gpu`, `cpu`, `cpu-fallback` yoki frameworklardan faqat
  bittasi CUDA ishlatayotgan xavfli `mixed`;
- `gpu_pipeline_ready`: Torch va Paddle ikkalasining deep tensor probe’i o‘tgan;
- `device_visible`: framework GPU’ni ko‘rgan-ko‘rmaganligi;
- `compute_ready` va `probe_ok`: real tensor amali ishlaganligi;
- `failure_codes`: masalan, secret/pathsiz `CUDNN_LOAD_FAILED`;
- `ocr_preload`: production PaddleOCR process modeli real yuklanganligi;
- `policy.cpu_fallback_applied`: so‘ralgan OCR GPU xavfsiz CPUga tushirilgan;
- package/driver/Python versiyalari.

CPU fallback xizmatning crash qilmasligini ta’minlaydi, lekin production line
throughput talabini kafolatlamaydi. NVIDIA serverda monitor
`gpu_pipeline_ready != true`, `ready != true` yoki `ocr_preload.ready != true`
holatini alert qilishi kerak. GPU talab qilingan runtime yoki preload buzilsa
`/health` HTTP 503 qaytaradi.

## Reproducible deployment evidence

Faqat toza environment va muvaffaqiyatli deep self-check’dan keyin host lock
yarating:

```bash
mkdir -p requirements/locks
python -m pip freeze --local > requirements/locks/ubuntu26-gpu-py311.txt
python -m pip check
python run.py --self-check --deep --require-gpu
```

Windows CPU lock va Linux CUDA lock bir-birini almashtirmaydi. Developer
kompyuterining to‘liq `pip freeze` natijasini root `requirements.txt`ga yozmang.

## Production acceptance

Runtime green bo‘lishi 99% OCR aniqlikning o‘zi emas. Release acceptance’da
quyidagilar alohida o‘lchanadi:

- runtime preflight va `/health` GPU green;
- belgilangan production crop regression setida VIN exact-match;
- line kamera/yoritish bo‘yicha end-to-end test;
- uzoq soak test, worker restart va CPU fallback alarm sinovi;
- deploy qilingan lock, model checksum va config snapshot arxivlangan.
