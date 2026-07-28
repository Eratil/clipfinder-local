# ClipFinder — instalacja dla testera

## Przed instalacją

- Windows 10 lub 11, 64-bit.
- Stabilne połączenie z internetem podczas instalacji i pierwszej analizy.
- Wolne miejsce na dysku: aplikacja, nagrania, eksporty i pobrany model AI mogą zajmować wiele gigabajtów.
- Karta NVIDIA jest opcjonalna. Bez niej aplikacja automatycznie działa w wolniejszym trybie CPU.

## Instalacja

1. Uruchom `ClipFinder-Setup-x.y.z.exe`.
2. Na końcu instalacji pozostaw zaznaczoną opcję konfiguracji komponentów Windows.
3. Zezwól instalatorowi na doinstalowanie brakujących składników: FFmpeg, Microsoft Edge WebView2 Runtime oraz Microsoft Visual C++ Redistributable. Wymagają one dostępu do internetu i korzystają z `winget`.
4. Uruchom ClipFinder z końca kreatora albo ze skrótu w menu Start.

Aplikacja zawiera własny Python oraz pakiety ClipFinder — tester nie musi osobno instalować Pythona ani pakietów `pip`.

## NVIDIA GPU (opcjonalnie)

Jeżeli otrzymałeś instalator z dopiskiem `-GPU`, podczas instalacji możesz zaznaczyć **Install NVIDIA GPU support**. Opcja uruchamia dołączone instalatory CUDA 12.9 i cuDNN 9.24; Windows może poprosić o zgodę administratora.

Wybierz ją tylko na komputerze z kartą NVIDIA. Bez tej opcji aplikacja zapisze profil CPU i nadal będzie działała, ale analiza będzie wolniejsza. Po ręcznej instalacji CUDA/cuDNN uruchom z menu Start **Configure ClipFinder runtime**, a następnie uruchom aplikację ponownie.

## Pierwsza analiza

Przy pierwszym użyciu wybrany model transkrypcji pobierze się lokalnie. Nie zamykaj aplikacji podczas analizy. Dla testów CPU instalator wybiera mniejszy model, aby pierwszy test był praktyczny; komputer z kompletnym CUDA 12 + cuDNN 9 automatycznie użyje GPU i modelu `large-v3`.

## Gdy coś nie działa

- Uruchom z menu Start **Configure ClipFinder runtime** jeszcze raz.
- Raport konfiguracji jest zapisany w `%LOCALAPPDATA%\ClipFinder\setup-status.txt`.
- Dane aplikacji, nagrania i eksporty są w `%LOCALAPPDATA%\ClipFinder\data`.
- Jeśli okno aplikacji nie otwiera się, sprawdź w raporcie WebView2 i uruchom ponownie Windows po jego instalacji.
