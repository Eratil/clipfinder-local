# ClipFinder — instalacja dla testera

## Przed instalacją

- Windows 10 lub 11, 64-bit.
- Stabilne połączenie z internetem podczas instalacji i pierwszej analizy.
- Wolne miejsce na dysku: aplikacja, nagrania, eksporty i pobrany model AI mogą zajmować wiele gigabajtów.
- Karta NVIDIA jest opcjonalna. Bez niej aplikacja automatycznie działa w wolniejszym trybie CPU.

## Instalacja

1. Uruchom `ClipFinder-Setup-x.y.z.exe`.
2. Na końcu instalacji pozostaw zaznaczoną opcję konfiguracji komponentów Windows.
3. Instalator zawiera Microsoft Visual C++ Redistributable. Zezwól konfiguratorowi na doinstalowanie brakujących FFmpeg i Microsoft Edge WebView2 Runtime przez `winget`; te składniki wymagają internetu.
4. Uruchom ClipFinder z końca kreatora albo ze skrótu w menu Start.

Aplikacja zawiera własny Python oraz pakiety ClipFinder — tester nie musi osobno instalować Pythona ani pakietów `pip`.

## NVIDIA GPU (opcjonalnie)

Podstawowy instalator ClipFinder nie wymaga karty NVIDIA i działa na CPU. Jeżeli otrzymałeś dodatkową paczkę `ClipFinder-GPU-Addon-<wersja dodatku>.exe`, uruchom ją dopiero po instalacji ClipFinder — wyłącznie na komputerze z kartą NVIDIA. Wersja dodatku jest niezależna od wersji aplikacji, więc nie trzeba instalować go ponownie po każdej aktualizacji ClipFindera.

Dodatek GPU zawiera plik `.exe` i dodatkowe pliki `.bin`. Wszystkie muszą pozostać w tym samym folderze aż do zakończenia instalacji — nie uruchamiaj samego `.exe` po przeniesieniu go osobno. Dodatek instaluje brakujące CUDA 12.9 i cuDNN 9.24, dopasowuje je według wersji i uruchamia prawdziwy test CTranslate2. Windows poprosi o zgodę administratora. Dodatek przyspiesza transkrypcję; wyszukiwanie podobieństwa nadal działa na CPU. Bez dodatku aplikacja pozostaje w pełni użyteczna, ale analiza jest wolniejsza.

## Pierwsza analiza

Przy pierwszym użyciu wybrany model transkrypcji pobierze się lokalnie. Nie zamykaj aplikacji podczas analizy, jeśli ma ona postępować bez przerwy; przerwane zadanie zostanie bezpiecznie odzyskane po ponownym uruchomieniu. Świeża konfiguracja CPU wybiera mniejszy model, aby pierwszy test był praktyczny; kompletna konfiguracja CUDA 12 + cuDNN 9 używa GPU i modelu `large-v3`. Aktualizacja zachowuje wcześniejszy wybór użytkownika.

Nagłówek **GPU READY** oznacza gotową transkrypcję CUDA. **CPU MODE** oznacza świadomie wybrany procesor, a **CPU FALLBACK** — że skonfigurowano CUDA, ale test środowiska GPU się nie udał. Informacja, że wyszukiwanie podobieństw działa na CPU, jest normalna dla podstawowej paczki i nie oznacza awarii transkrypcji GPU.

## Aktualizacje

ClipFinder cicho sprawdza dostępność aktualizacji przy starcie i pokazuje mały komunikat, gdy znajdzie nowszą wersję. Szczegóły oraz ręczny przycisk są w zakładce **Opcje**. Bezpośrednio poprzednia wersja może pobrać małą aktualizację; starsza wersja bezpiecznie pobiera pełny instalator. Po weryfikacji pliku aplikacja zamyka się, aktualizuje i uruchamia ponownie. Aktualizacja nie usuwa `%LOCALAPPDATA%\ClipFinder\data` ani pobranych modeli.

## Gdy coś nie działa

- Uruchom z menu Start **Configure ClipFinder runtime** jeszcze raz.
- Raport konfiguracji jest zapisany w `%LOCALAPPDATA%\ClipFinder\setup-status.txt`.
- W aplikacji otwórz **Opcje → Raport diagnostyczny**. Możesz skopiować lub zapisać raport i przekazać go osobie diagnozującej problem; raport nie zawiera nagrań, transkryptów, czatu ani promptów.
- Szczegółowy lokalny log znajduje się w `%LOCALAPPDATA%\ClipFinder\data\logs\clipfinder.log`.
- Dane aplikacji, nagrania i eksporty są w `%LOCALAPPDATA%\ClipFinder\data`.
- Jeśli okno aplikacji nie otwiera się, sprawdź w raporcie WebView2 i uruchom ponownie Windows po jego instalacji.

## Zwalnianie miejsca na dysku

Przycisk **Usuń film źródłowy** działa tylko ręcznie i usuwa duży plik nagrania z katalogu ClipFindera. Zostawia analizę, transkrypcję, tagi, reakcje czatu, decyzje oraz dane uczące. Dla każdego fragmentu zawierającego ręczną ocenę, sprawdzony tag, edycję lub użycie jako wzorzec zapisuje mały plik MP3 konkretnej ocenionej wersji, który nadal można odsłuchać. Po usunięciu źródła nie można już ponownie przeanalizować nagrania, otworzyć całej transmisji ani wyeksportować nowego MP4.
