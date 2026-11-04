# darktable-mcp

Serwer MCP do obróbki zdjęć: zapisuje edycje jako XMP darktable i renderuje je przez `darktable-cli`.
Użytkownik ma darktable **5.6.1** (Windows, workflow „scene-referred (sigmoid)”).

## Środowisko i testy

- Sesja w chmurze: zanim zaczniesz pracę, uruchom `bash scripts/cloud-setup.sh` (darktable-cli 5.6.1, jeśli setup
  script środowiska go nie zainstalował; zależności; próbki RAW z Canona 450D i Olympusa E-M10 III).
- `python -m pytest tests -q`: render przez prawdziwy darktable-cli i sprawdzenie, czy darktable wczytał każdy
  zapisany moduł bez odrzucenia (wersja i rozmiar parametrów z logu `-d params`).
- Każdy nowy lub zmieniony moduł w `xmp_writer.py` dostaje test: XMP się wczytuje (wzór w
  `test_darktable_accepts_every_written_module`) oraz pomiar renderu pokazuje oczekiwany kierunek efektu.

## Styl kodu

- Jak najmniej linii; zwarte, idiomatyczne konstrukcje. Bez abstrakcji na zapas i defensywnych gałęzi.
- Komentarze tylko „dlaczego”, nie „co”.
- Najpierw sprawdź, czy da się to zrobić w istniejącym kodzie, zanim dodasz plik albo helper.

## Kodowanie modułów w XMP

Pisanej specyfikacji XMP nie ma; specyfikacją jest kod źródłowy dokładnie tej wersji:
`https://raw.githubusercontent.com/darktable-org/darktable/release-5.6.1/src/...`
- `common/exif.cc`: blob jako hex albo `gz` + 2 cyfry (współczynnik kompresji, max 99) + base64 strumienia zlib.
- `iop/<moduł>.c`: `dt_iop_<moduł>_params_t` (kolejność pól, `$DEFAULT`), wersja w `DT_MODULE_INTROSPECTION`.
- `develop/masks.h`, `develop/blend.h` (`dt_develop_blend_params_t` v14, `mask_id` na offsecie 24).
- Nie zgaduj układu bajtów. Moduł wybieraj z podręcznika (docs.darktable.org/usermanual/5.6/en/module-reference/),
  wartości startowe bierz z wbudowanych presetów (`init_presets` w `iop/<moduł>.c`).
- W 5.6.1 balans bieli przy scene-referred robi color calibration (channelmixerrgb), nie temperature.
- `darktable-cli`: ścieżki z `/`; opcje własne (`--verbose`) przed `--core`, opcje darktable (`-d params`,
  `--configdir`) po nim; zawsze `--library :memory:` i tymczasowy `--configdir`. Kod wyjścia jest 0 także przy błędzie.

Sprawdzone układy (little-endian, 5.6.1):
- sigmoid v3 `<4fi f6f fi` (56 B): contrast, skew, white target 100, black target 0.0152, color mode (0 = per channel),
  hue preservation %, 6 insetów/rotacji primaries, purity, base primaries.
- colorbalancergb v5 `<32fi` (132 B): shadows/midtones/highlights/global Y,C,H; shadows_weight, white_fulcrum,
  highlights_weight; chroma shadows/highlights/global/midtones; saturation global/highlights/midtones/shadows;
  hue_angle; brilliance global/highlights/midtones/shadows; mask_grey_fulcrum 0.1845; vibrance; grey_fulcrum 0.1845;
  contrast; saturation_formula (int).
- blend params v14 `<3I2f3IfI4fI2I64f16f20s3i` (420 B): mask_mode (1 | 2 drawn | 4 parametric), blend_cst 4 (RGB scene),
  blend_mode 24 (normal), parameter, opacity %, mask_combine (1 = invert), mask_id (grupa), blendif (bit kanału; +16 =
  odwrócona polaryzacja), feathering radius, guide (5 = input after blur), blur, contrast, brightness, details,
  feather_version 1, reserved×2, 16×4 parametry blend-if, 16 boost (Jz/Cz = -6.64385619), raster source, instance,
  raster id -1, raster invert. Wartość suwaka = param × 2^boost × 100.
- gradient (mask_type 16) `<6fi`: anchor x/y (pełna klatka sensora, przed flip; orientacja = `rawpy sizes.flip`, ale z zamienionymi
  5↔6: EXIF 6 to LibRaw 6, a darktable 5 = ROTATE_CW_90_DEG), rotation (0 = maska u góry sensora, 90 = lewo), compression 0–1 (erf, w przekątnych), steepness, curvature, state 2 (sigmoidal); grupa (mask_type 4) `<3if`: formid, parentid, state 3, opacity.
  Dodatkowe instancje (multi_priority > 0) wymagają `iop_order_list`, inaczej darktable je pomija.
- graduatednd v1 `<6f`: density EV, hardness %, rotation, offset %, hue, saturation. Rotacja 0 przyciemnia górę;
  suwak w GUI pokazuje -rotation.

## Wygląd (domyślne wartości, które użytkownik akceptuje)

- Sigmoid zamiast filmic; kontrast z domyślnego 1,5, głębia przez lekki black w exposure (+0,005…+0,02).
- Bez local contrast i bez automatycznej winiety; skóra gładka, nie wyostrzona.
- Korekty koloru w światłach/półtonach, nie globalnym offsetem (barwi czernie).

## Pliki użytkownika

- Nigdy nie nadpisuj XMP zapisanego przez darktable ani zawierającego zmiany użytkownika (retouch, maski).
- Nazwa XMP: pełna nazwa RAW + `.xmp` (`IMG_1.CR2.xmp`), duplikaty `IMG_1_01.CR2.xmp`.
