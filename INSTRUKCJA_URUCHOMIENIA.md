# ClipFinder Local - uruchomienie na innym komputerze

Ta instrukcja opisuje, jak uruchomic aplikacje ClipFinder Local na innym komputerze z Windowsem.

## 1. Co przekazac drugiej osobie

Przekaz caly katalog `clipfinder-local`, ale **nie kopiuj katalogu `.venv`**. Srodowisko wirtualne Pythona jest przypisane do konkretnego komputera.

Katalog `data` nie jest potrzebny do pierwszego uruchomienia. Zawiera lokalna baze, nagrania, podglady i eksporty. Jezeli chcesz przekazac tylko gotowe pliki, skopiuj osobno `data\exports`.

Nie kopiuj rowniez katalogu `analysis_frames`, jezeli jest obecny. Zawiera on techniczne pliki pomocnicze z testow.

## 2. Wymagania komputera

- Windows 10 lub Windows 11
- Python **3.11, 64-bit**
- FFmpeg z programem `ffprobe` dostepne w zmiennej `PATH`
- Zalecane: karta NVIDIA z co najmniej 8 GB VRAM
- Dla analizy na GPU: CUDA 12.x z cuBLAS oraz cuDNN 9
- Dostep do Internetu przy pierwszym uruchomieniu. Modele AI zostana pobrane lokalnie tylko raz.

Aplikacja moze dzialac bez karty NVIDIA, ale analiza bedzie znacznie wolniejsza. Instrukcja trybu CPU jest w sekcji 7.

## 3. Instalacja Pythona i FFmpeg

1. Zainstaluj Python 3.11 64-bit z [python.org](https://www.python.org/downloads/release/python-3119/).
2. Podczas instalacji zaznacz opcje `Add Python to PATH`.
3. Otworz nowe okno PowerShell i sprawdz:

```powershell
py -3.11 --version
ffmpeg -version
ffprobe -version
```

Kazde z polecen powinno wyswietlic wersje. Jezeli `ffmpeg` lub `ffprobe` nie zostana znalezione, zainstaluj pelny pakiet FFmpeg i dodaj jego folder `bin` do zmiennej `PATH`.

## 4. Przygotowanie aplikacji

### Wariant prosty - automatyczny instalator

W nowej kopii aplikacji wystarczy dwukrotnie kliknac `Install-ClipFinder.cmd`. Skrypt automatycznie sprawdzi i, gdy brakuje, doinstaluje przez `winget` Python 3.11 oraz FFmpeg, utworzy `.venv`, zainstaluje pakiety z `requirements.txt` i uruchomi diagnostyke.

CUDA 12.x i cuDNN 9 wymagaja nadal recznej instalacji zgodnej ze sterownikiem NVIDIA. Na komputerze bez GPU uruchom w PowerShell:

```powershell
.\Install-ClipFinder.ps1 -CpuFallback
```

### Wariant reczny

1. Rozpakuj katalog `clipfinder-local`, na przyklad do `C:\ClipFinder\clipfinder-local`.
2. Otworz PowerShell w tym katalogu.
3. Wklej kolejno:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

4. Sprawdz instalacje:

```powershell
python scripts\doctor.py
```

Przy poprawnym GPU skrypt powinien pokazac wykryta karte NVIDIA oraz brak komunikatow `[MISSING]` dla CUDA 12 cuBLAS i cuDNN 9.

## 5. CUDA i cuDNN dla analizy na GPU

Ten krok jest potrzebny tylko dla analizy na NVIDIA GPU.

1. Zainstaluj aktualny sterownik NVIDIA.
2. Zainstaluj CUDA Toolkit 12.x. CUDA 13 sama w sobie nie wystarcza dla aktualnej wersji faster-whisper.
3. Zainstaluj cuDNN 9 dla CUDA 12.
4. Skopiuj pliki DLL cuDNN 9 do folderu podobnego do:

```text
C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.x\bin
```

5. Uruchom ponownie:

```powershell
.\.venv\Scripts\Activate.ps1
python scripts\doctor.py
```

Nie zmieniaj nazw plikow DLL CUDA 13 ani nie nadpisuj nimi DLL z CUDA 12.

## 6. Uruchomienie aplikacji

Po jednorazowej instalacji wystarczy dwukrotnie kliknac:

```text
Start-ClipFinder-Desktop.cmd
```

ClipFinder otworzy sie w osobnym oknie aplikacji i uruchomi lokalny serwer w tle. Zamykaj okno dopiero po zakonczeniu uploadu, analizy lub eksportu.

Plik `Start-ClipFinder.cmd` pozostaje dostepny do diagnostyki. Otwiera on okno PowerShell oraz wersje przegladarkowa pod adresem:

```text
http://127.0.0.1:8000
```

Po zmianie kodu aplikacji zamknij cale okno ClipFinder i uruchom `Start-ClipFinder-Desktop.cmd` ponownie.

## 7. Tryb CPU, gdy nie ma NVIDIA lub CUDA

Otworz plik `.env` w katalogu aplikacji i ustaw:

```text
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
```

Zapisz plik i uruchom ponownie `Start-ClipFinder.cmd`. Analiza bedzie dzialala, ale znacznie wolniej niz na GPU.

## 8. Pierwsze uzycie

1. W panelu bocznym ustaw domyslny uklad eksportu oraz sciezke audio. Dla obecnych nagran domyslnie poprawna jest `Track 1 - microphone + game`.
2. Wgraj nagranie MP4 albo wklej publiczny link YouTube lub Twitch VOD. Link zostanie pobrany lokalnie, a postep pojawi sie na liscie nagran. Pobieraj tylko materialy, do ktorych masz prawo.
3. Analiza trwa w tle. Strone mozna zamknac po zakonczeniu uploadu, ale okno launchera musi zostac otwarte.
4. Otworz nagranie, przesluchaj fragmenty i zatwierdz wybrane klipy.
5. Przed eksportem ustaw pozycje napisow, opcjonalna nazwe pliku oraz, jezeli potrzeba, przelacznik cenzurowania przeklenstw.
6. Kliknij `Export MP4`.

## 8a. Oddzielna analiza mikrofonu i dzwiekow gry

W panelu **Saved setup -> Clip setup -> Analysis audio sources** wybierz tryb `Separate microphone and sound tracks`, gdy nagranie ma osobne sciezki. Transkrypcja, napisy i prompty beda wtedy tworzone tylko z mikrofonu, a wybrane sciezki `all sounds` i `only game` posluza tylko do wykrywania dynamicznych zdarzen dzwiekowych podbijajacych ranking klipow.

Dla obecnych nagran domyslnie ustawiono: Track 1 = wszystkie dzwieki, Track 2 = mikrofon, Track 3 = sama gra. Ustawienia dzialaja dla kolejnej analizy albo `Reanalyze recording`.

## 9. Wazne informacje o ukladach pionowych

Trzy pionowe uklady zostaly przygotowane pod nagrania 1920x1080, w ktorych kamera jest w ramce w prawym gornym rogu, a gra zajmuje pozostala czesc obrazu.

Na innym kanale lub przy innym ukladzie sceny OBS aplikacja bedzie dzialac, ale kadrowanie kamery i gry moze wymagac dostrojenia w kodzie.

## 10. Najczestsze problemy

| Problem | Rozwiazanie |
| --- | --- |
| `python` nie jest znaleziony | Zamknij i otworz PowerShell ponownie. Uzyj `py -3.11` zamiast `python`. |
| Brak `ffmpeg` lub `ffprobe` | Dodaj folder `bin` FFmpeg do `PATH`, potem otworz nowe okno PowerShell. |
| `[MISSING] CUDA 12 cuBLAS` lub `cuDNN 9` | Zainstaluj CUDA 12.x i cuDNN 9, a DLL umiesc w folderze CUDA 12 `bin`. |
| Strona nie odpowiada | Sprawdz, czy okno launchera jest otwarte. Uruchom ponownie `Start-ClipFinder.cmd`. |
| Eksport nie ma prawidlowego dzwieku | W panelu bocznym wybierz `Track 1 - microphone + game`, zapisz ustawienia i wyeksportuj klip ponownie. |
| Uklad pionowy zle kadruje obraz | Nagranie ma inny uklad OBS niz domyslny. Potrzebne jest dostrojenie kadru kamery. |
| Link YouTube lub Twitch VOD nie pobiera sie | Sprawdz, czy material jest publiczny i czy masz prawo go pobrac. Zaktualizuj pobieranie przez `python -m pip install -U yt-dlp`. |
