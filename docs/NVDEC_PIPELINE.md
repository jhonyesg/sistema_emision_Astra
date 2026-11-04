# Pipeline NVDEC (decodificar y escalar en la GPU NVIDIA)

Desde el 27/10/2026 la plataforma Astra usa la **GT 1030** como aceleradora de
**decodificación y escalado**, dejando la **iGPU (UHD 630) solo para codificar**.

## Por qué esta topología

La GT 1030 (GP108) **no tiene motor NVENC**: no puede codificar video. Lo que sí
tiene es **NVDEC** (decodificación por hardware) y **CUDA**. Entonces el
reparto posible es:

```
fuente ─► NVDEC (NVIDIA) ─► scale_cuda (NVIDIA) ─► RAM ─► hwupload ─► h264_vaapi (iGPU) ─► RTMP
           decode + escala                            (pipe NUT)      encode, calidad intacta
```

* **CPU**: se queda sin decodificar H.264/MPEG-2 ni escalar (las dos tareas
  mas caras). Medido: **-46 % de CPU del sistema** (54,5 % → 29 %).
* **iGPU**: sigue haciendo lo unico que no se puede mover (el encode), pero sin
  el trabajo de escalado (VPP) de los canales que antes escalaban en la iGPU.
* **Calidad**: sin cambios. Se conserva el mismo encoder, bitrate, `-quality 4`
  y GOP. El escalado `scale_cuda:interp_algo=bilinear` quedo **mas cerca del
  bilinear ideal** que el `fast_bilinear` de swscale (PSNR 55,2 dB vs 49,4 dB).
  Bitrate medido de los canales migrados: 1933 kbps vs 1910 kbps de los no
  migrados (+1,2 %, dentro del ruido de la tasa variable).

## Requisitos

1. Driver NVIDIA propietario instalado y `persistence mode` **activo**.
   Sin persistence mode la GeForce descarga y recarga la GPU cada segundo y
   CUDA falla con `CUDA_ERROR_UNKNOWN`:

   ```bash
   nvidia-smi --query-gpu=persistence_mode --format=csv
   # -> Enabled
   ```

   El servicio queda fijado con un drop-in:
   `/etc/systemd/system/nvidia-persistenced.service.d/override.conf`
   (`StopWhenUnneeded=false` y sin `--no-persistence-mode`).

2. La NVIDIA no debe tomar el escritorio (esta caja es headless):
   `/etc/modprobe.d/nvidia-drm-headless.conf` -> `options nvidia-drm modeset=0`.

3. ffmpeg con los decodificadores `cuvid` (los trae el ffmpeg de Ubuntu:
   `ffmpeg -decoders | grep cuvid`).

## Como se activa

Por stream, con la clave `DECODER=nvidia` en la seccion del INI:

```ini
[<canal>]
Original_URL=http://192.168.0.<lan>:8000/play/a091
Destination_URL=rtmp://127.0.0.1:1935/live/<canal>
VAAPI_DRIVER=iHD
DECODER=nvidia
```

Desde la UI o la API (escribe el INI y reinicia ese stream):

```bash
curl -X POST -H "Content-Type: application/json" \
     -d '{"DECODER":"nvidia"}' \
     http://127.0.0.1:5006/api/stream/<canal-<canal>>/update
```

Para volver a la cadena clasica (decode por CPU):

```bash
curl -X POST -H "Content-Type: application/json" \
     -d '{"DECODER":""}' \
     http://127.0.0.1:5006/api/stream/<canal-<canal>>/update
```

## Que se ve en el supervisor

* `api/status` -> campo `pipeline`: `nvidia-nvdec` o `cpu-decode`.
* `api/stream/<n>/command` -> muestra las dos etapas.
* `api/logs/<n>` -> las lineas de la etapa NVIDIA van marcadas con `[nv]`.
* Procesos: por stream hay **dos** ffmpeg (P1=NVDEC, P2=encode) y un pipe
  NUT en `/tmp/astra_nv/<stream>.<pid>.<ts>.nut`.

## Fuentes que NO pueden usar este pipeline

Probados uno por uno (etapa 1 real, 12 s cada fuente):

| Canal | Fuente | Veredicto | Motivo |
|---|---|---|---|
| `<canal-A>` | a06w | **no migrable** | el decoder NVDEC muere con **SIGSEGV** (rc=139) al decodificar + `bwdif_cuda`; la fuente es MPEG-2 con cabeceras danadas (`Invalid frame dimensions 0x0`) |
| `<canal-B>` | a06x | **no migrable** | igual que el anterior (SIGSEGV con `bwdif_cuda`) |
| `<canal-C>` | a07i | **no migrable** | sin deinterlace, pero el decoder NVDEC no logra decodificar la fuente (rc=234, 0 frames) |
| `<canal-D>` | a07o | **migrable** (migrado) | produce 330 frames en 12 s; el aviso de dimensiones no impide el decode |

Los tres que quedaron fuera siguen en la cadena clasica, donde el decode por
software **disimula** los frames rotos (el deinterlace por software tambien).
Un SIGSEGV en la etapa 1 no rompe nada: el supervisor lo detecta, marca el
stream como caido y lo reinicia, pero en bucle.

Estos dos streams quedaron en `cpu-decode`. **Regla**: antes de migrar un canal,
probar la etapa 1 sola sobre su fuente y confirmar que produce frames:

```bash
ffmpeg -y -re -timeout 10000000 -fflags +discardcorrupt+flush_packets -err_detect ignore_err \
  -hwaccel cuvid -hwaccel_device 0 -hwaccel_output_format cuda \
  -i <URL> -t 10 -vf "scale_cuda=720:408:interp_algo=bilinear,hwdownload,format=nv12" \
  -c:v rawvideo -c:a copy -f nut /dev/null
```

Los relays `-c copy` (los `*_vps`) **no se tocan**: no decodifican nada.

## Monitoreo

```bash
# carga de CPU e iGPU del sistema
ps -eo pcpu,args | grep "[f]fmpeg" | awk '{s+=$1} END {print s" % ffmpeg"}'
sudo intel_gpu_top -l 1          # motores de la iGPU (RCS = encode, VCS = decode)
nvidia-smi --query-gpu=utilization.gpu,utilization.encoder,memory.used --format=csv
```

## Limites conocidos

* La GPU NVIDIA no puede codificar: no hay motor NVENC en GP108, y la API de
  encode de VDPAU no existe en `libvdpau` 1.5 de Ubuntu 24.04. Si en el futuro
  se necesita mover *encodes* fuera de la iGPU, hay que cambiar la tarjeta
  (GTX 1050 Ti / Quadro P620 / Tesla T4).
* La iGPU queda como cuello de botella real: 23 encodes a `-quality 4` la
  dejan en 85-95 % de su motor de render.
* La tasa de sesiones NVDEC simultaneas la limita la VRAM (2 GB): con las
  fuentes reales del plantel (720x480) se sostuvieron 34 decodes simultaneos.
