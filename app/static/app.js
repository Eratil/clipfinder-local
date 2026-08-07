const state = { videoId: null, collectionId: null, collectionName: '', videos: [], rejectionReasons: [], analysisAudio: { mode:'split', single_track:1, microphone_track:2, all_sounds_track:1, game_track:3, use_all_sounds:true, use_game:true }, discovery: { active_profile:'general', pattern_set_id:'', profanity_filter:'allow', pattern_sets:[], profiles:[] }, chat: null, resultMode: 'all', activeResults: null, loadedReadyVideoId: null, previewAudio: null, listeningSegment: null, listenAudioTrack: 1, quickReview: { clips: [], index: 0, saving: false, previewKey: '' }, remotePreview: { jobId:null, completedJobId:null, poll:null }, editingSegment: null, clipEditorOpen: false, editorTab: 'edit', captionPositions: {}, exportNames: {}, globalCaption: { captions_preset: 'highlight', base_color: '#FFFFFF', active_color: '#FFFF00', outline_enabled: true, outline_color: '#000000', glow_enabled: false, opacity: 100 }, globalExport: { layout: 'original', layout_preset_id: '', audio_track: 1, camera_x:.78, camera_y:.03, camera_width:.11, camera_height:.11, game_x:.22, game_y:0, game_width:.56, game_height:1 }, layoutPresets: [], layoutCalibration: { mode:'camera', drawing:null }, captionDirty: false, exportDirty: false, analysisAudioDirty: false, discoveryDirty: false, statusErrorUntil: 0, updateDownloadId: null, updatePollTimer: null, updatePollGeneration: 0, appVersion: '', videoRequestGeneration: 0, segmentRequestGeneration: 0, chatRequestGeneration: 0, importRequestGeneration: 0, resultRequestGeneration: 0, quickReviewRequestGeneration: 0, listeningRequestGeneration: 0, dashboardRefreshPromise: null, runtimeStatusPromise: null, storage: null, storageLoadedAt: 0, videoRenderSignature: '', importRenderSignature: '', hasActiveVideoJobs: false, hasActiveImports: false };
// One preview definition per export preset. Add a new entry here when adding a
// caption preset, so the settings preview stays in sync without a new CSS rule.
const captionPreviewPresets = {
  none: { showText:false, hint:'Captions are disabled for export.' },
  clean: { size:'25px', weight:'700', active:false, activeScale:'1', activeWeight:'700', outline:3, hint:'Clean: medium text with a strong outline. The active-word colour is not used by this preset.' },
  highlight: { size:'29px', weight:'700', active:true, activeScale:'1.12', activeWeight:'900', outline:4, hint:'Word highlight: the currently spoken word changes colour and becomes larger.' },
  minimal: { size:'19px', weight:'400', active:false, activeScale:'1', activeWeight:'400', outline:2, hint:'Minimal: smaller, lighter text with a subtle outline. The active-word colour is not used by this preset.' },
  boxed_pop: { size:'26px', weight:'800', active:false, activeScale:'1', activeWeight:'800', outline:2, variant:'boxed', hint:'Boxed Pop: bold text in a compact caption card.' },
  neon_gaming: { size:'28px', weight:'800', active:true, activeScale:'1.16', activeWeight:'900', outline:2, variant:'neon', hint:'Neon Gaming: gaming-style text; the spoken word is larger and uses the active colour.' },
  cinematic: { size:'22px', weight:'400', active:false, activeScale:'1', activeWeight:'400', outline:2, variant:'cinematic', hint:'Cinematic: understated text on a dark subtitle band.' },
  karaoke_punch: { size:'30px', weight:'800', active:true, activeScale:'1.24', activeWeight:'900', outline:4, variant:'karaoke', hint:'Karaoke Punch: each spoken word uses a stronger bounce and active colour.' },
  minimal_center: { size:'20px', weight:'400', active:false, activeScale:'1', activeWeight:'400', outline:2, variant:'minimal-center', hint:'Minimal Center: quiet, centered text. This preset forces the center position in exported clips.' }
};
const captionPreviewWords = ['These', 'are', 'test', 'captions'];
const captionPreviewFontFamilies = Object.fromEntries(['Inter', 'Montserrat', 'Poppins', 'Lato', 'Roboto Condensed', 'Oswald', 'Nunito', 'Noto Sans', 'Bungee', 'Cinzel', 'Pixelify Sans'].map((name) => [name, `"ClipFinder ${name}", Arial, sans-serif`]));
let captionPreviewTimer = null;
const $ = (selector) => document.querySelector(selector);
const APP_LANGUAGE_KEY = 'clipfinder-interface-language';
const polishText = {
  'Saved setup':'Zapisane ustawienia', 'Search tools and global settings - available at any time.':'Narzędzia wyszukiwania, ustawienia klipów i opcje aplikacji.', 'Hide':'Ukryj', 'Search':'Wyszukiwanie', 'Global':'Globalne', 'Statistics':'Statystyki', 'Options':'Opcje',
  'Application language':'Język aplikacji', 'Changes the ClipFinder interface only. Analysis models, tags and saved data stay unchanged.':'Zmienia wyłącznie interfejs ClipFindera. Modele analizy, tagi i zapisane dane pozostają bez zmian.', 'Interface language':'Język interfejsu', 'English':'Angielski',
  'Exact text search':'Dokładne wyszukiwanie tekstu', 'Find text':'Znajdź tekst', 'Saved prompts':'Zapisane prompty', 'Save prompt':'Zapisz prompt', 'Active description prompt':'Aktywny prompt opisu', 'Search active prompt':'Szukaj aktywnego promptu',
  'Reference collections':'Kolekcje wzorców', 'Create collection':'Utwórz kolekcję', 'Clip collections':'Kolekcje klipów', 'Find similar to active collection':'Znajdź podobne w aktywnej kolekcji', 'Generate prompt from active collection':'Wygeneruj prompt z aktywnej kolekcji', 'Folder with ready-made clips':'Folder z gotowymi klipami', 'Include subfolders':'Uwzględnij podfoldery', 'Save folder and import':'Zapisz folder i zaimportuj', 'Destination collection':'Kolekcja docelowa', 'Import link as reference':'Zaimportuj link jako wzorzec',
  'Single Short/video preview':'Podgląd pojedynczego Shorta/filmu', 'Analyze temporary preview':'Przeanalizuj tymczasowy podgląd',
  'Discovery profile':'Profil wyszukiwania', 'Changes how candidates are ranked. It uses your approvals and rejection reasons too.':'Zmienia sposób rankingu kandydatów. Uwzględnia także Twoje akceptacje i powody odrzucenia.', 'Content type':'Typ treści', 'Pattern add-on':'Dodatek wzorców', 'Profanity in search':'Wulgaryzmy w wyszukiwaniu', 'Allow all':'Dopuszczaj wszystkie', 'Allow up to one per clip':'Dopuszczaj maksymalnie jedno na klip', 'Hide all clips with profanity':'Ukryj wszystkie klipy z wulgaryzmami', 'Save discovery profile':'Zapisz profil wyszukiwania', 'Create pattern set':'Utwórz zestaw wzorców', 'Saved pattern sets':'Zapisane zestawy wzorców', 'A pattern set stores only conclusions from reviewed Shorts: semantic vector, tags and scores. It never stores the source video, audio or frame.':'Zestaw wzorców zapisuje wyłącznie wnioski z przejrzanych Shortów: wektor semantyczny, tagi i oceny. Nigdy nie zapisuje źródłowego filmu, audio ani klatki.',
  'Custom rejection reasons':'Własne powody odrzucenia', 'Saved reasons appear in the rejection list on every clip.':'Zapisane powody pojawiają się na liście odrzucenia każdego klipu.', 'Save reason':'Zapisz powód', 'Saved rejection reasons':'Zapisane powody odrzucenia',
  'Caption defaults':'Domyślne napisy', 'Used for exports in the current session and restored when ClipFinder is reopened. Save a named favorite below to keep a reusable preset.':'Używane przy eksporcie w bieżącej sesji i przywracane po ponownym otwarciu ClipFindera. Zapisz niżej nazwany ulubiony preset, aby używać go ponownie.', 'Caption type':'Typ napisów', 'Caption font':'Czcionka napisów', 'Sentence colour':'Kolor zdania', 'Spoken word colour':'Kolor wypowiadanego słowa', 'Add outline':'Dodaj obrys', 'Outline colour':'Kolor obrysu', 'Add light glow':'Dodaj lekką poświatę', 'Text opacity':'Przezroczystość tekstu', 'Preview background':'Tło podglądu', 'Custom background colour':'Własny kolor tła', 'Save current colours as favorite':'Zapisz bieżące kolory jako ulubione', 'Saved caption colours and fonts':'Zapisane kolory i czcionki napisów',
  'Export defaults':'Domyślne ustawienia eksportu', 'Used for exports in the current session and restored when ClipFinder is reopened. Save a named layout below to keep a reusable preset.':'Używane przy eksporcie w bieżącej sesji i przywracane po ponownym otwarciu ClipFindera. Zapisz niżej nazwany układ, aby używać go ponownie.', 'Clip layout':'Układ klipu', 'Original audio track':'Oryginalna ścieżka dźwiękowa', 'Layout calibration and preview':'Kalibracja i podgląd układu', 'Select a recording, pause on a representative frame, choose an area and drag a rectangle. The preview is shown before export.':'Wybierz nagranie, zatrzymaj je na reprezentatywnej klatce, wybierz obszar i przeciągnij prostokąt. Podgląd jest widoczny przed eksportem.', 'Use selected recording':'Użyj wybranego nagrania', 'Draw camera area':'Zaznacz obszar kamery', 'Draw gameplay area':'Zaznacz obszar gry', 'Choose a recording first.':'Najpierw wybierz nagranie.', 'Save current layout':'Zapisz bieżący układ', 'Saved layouts':'Zapisane układy', 'Analysis audio sources':'Źródła audio analizy', 'Used for the next analysis or reanalysis. Captions and prompt search always use the selected transcription track.':'Używane przy następnej analizie lub reanalizie. Napisy i wyszukiwanie promptów zawsze korzystają z wybranej ścieżki transkrypcji.', 'One track - legacy analysis':'Jedna ścieżka - analiza klasyczna', 'Separate microphone and sound tracks':'Oddzielne ścieżki mikrofonu i dźwięku', 'Single track for transcription':'Jedna ścieżka do transkrypcji', 'Only microphone - transcription track':'Tylko mikrofon - ścieżka transkrypcji', 'Use all sounds for event scoring':'Użyj wszystkich dźwięków do oceny zdarzeń', 'All sounds track':'Ścieżka wszystkich dźwięków', 'Use only game sounds for event scoring':'Użyj tylko dźwięków gry do oceny zdarzeń', 'Only game track':'Tylko ścieżka gry', 'Save analysis audio settings':'Zapisz ustawienia audio analizy',
  'Application language':'Język aplikacji', 'Interface language':'Język interfejsu', 'Software updates':'Aktualizacje programu', 'Check for updates':'Sprawdź aktualizacje', 'Download update':'Pobierz aktualizację', 'Download manually':'Pobierz ręcznie', 'Open diagnostic report':'Otwórz raport diagnostyczny',
  'Review statistics':'Statystyki decyzji', 'All locally analysed recordings. Use this to see what the ranking gets right or wrong.':'Wszystkie lokalnie przeanalizowane nagrania. Użyj tego widoku, aby sprawdzić, co ranking wybiera poprawnie, a co błędnie.', 'Refresh':'Odśwież', 'Why clips are rejected':'Dlaczego klipy są odrzucane', 'Approved vs rejected scores':'Oceny: zaakceptowane kontra odrzucone', 'A large gap means this score helps selection. A small or reversed gap is a signal that it needs tuning.':'Duża różnica oznacza, że dana ocena pomaga w selekcji. Mała lub odwrócona różnica sugeruje konieczność dostrojenia.', 'Tags and decisions':'Tagi i decyzje', 'Shows the most frequent tags and how often clips with that tag are approved or rejected.':'Pokazuje najczęstsze tagi oraz to, jak często klipy z danym tagiem są zatwierdzane lub odrzucane.', 'Analysis modes and reading filter':'Tryby analizy i filtr czytania',
  'New recording':'Nowe nagranie', 'Analysis mode':'Tryb analizy', 'Upload and analyze':'Wgraj i analizuj', 'Download link and analyze':'Pobierz link i analizuj', 'Paste a public YouTube link or Twitch VOD URL only when you are allowed to download it. The page can be closed after upload or link submission; keep the launcher window open while processing runs.':'Wklej publiczny link YouTube lub Twitch VOD tylko wtedy, gdy masz prawo go pobrać. Stronę można zamknąć po wysłaniu pliku lub linku, ale okno uruchamiające musi pozostać otwarte podczas przetwarzania.', 'Recordings':'Nagrania', 'Refresh now':'Odśwież', 'Candidates':'Kandydaci',
  'Chat reaction analysis':'Analiza reakcji czatu', 'Import a chat transcript with timestamps from the start of the recording. Messages are scored after your clip, using the delay below.':'Zaimportuj transkrypcję czatu ze znacznikami czasu liczonymi od początku nagrania. Wiadomości są oceniane po klipie z uwzględnieniem opóźnienia poniżej.', 'Chat transcript (.json, .csv, .tsv or .txt)':'Transkrypcja czatu (.json, .csv, .tsv lub .txt)', 'Expected chat delay (seconds)':'Przewidywane opóźnienie czatu (sekundy)', 'Import chat and score clips':'Zaimportuj czat i oceń klipy', 'Save delay and recalculate':'Zapisz opóźnienie i przelicz',
  'Tag':'Tag', 'All tags':'Wszystkie tagi', 'Possible reading':'Możliwe czytanie', 'Hide possible reading':'Ukryj możliwe czytanie', 'Show similar alternatives':'Pokaż podobne warianty', 'Find tag':'Znajdź tag', 'Clip status':'Status klipu', 'All clips':'Wszystkie klipy', 'Approved clips':'Zaakceptowane klipy', 'Not reviewed':'Nieprzejrzane', 'Rejected clips':'Odrzucone klipy', 'Filter status':'Filtruj status', 'Sort results':'Sortuj wyniki', 'Best of stream':'Najlepsze ze streama', 'Show Best of stream':'Pokaż najlepsze ze streama', 'Quick selection':'Szybka selekcja',
  'Open in full recording':'Otwórz w całym nagraniu', 'Approve clip':'Zatwierdź klip', 'Reject':'Odrzuć', 'Listen':'Odsłuchaj', 'Add as reference':'Dodaj jako wzorzec', 'Click this clip to edit it in the right panel.':'Kliknij klip, aby edytować go w prawym panelu.',
  'Clip editor':'Edytor klipu', 'Detailed scoring':'Szczegółowa ocena', 'Click any candidate card to edit its range, captions, review status and export.':'Kliknij dowolną kartę kandydata, aby edytować zakres, napisy, status i eksport.', 'Start (seconds)':'Początek (sekundy)', 'End (seconds)':'Koniec (sekundy)', 'Save range':'Zapisz zakres', 'Caption position':'Pozycja napisów', 'Bottom':'Dół', 'Middle':'Środek', 'Top':'Góra', 'Clip status':'Status klipu', 'Not reviewed':'Nieprzejrzane', 'Approved':'Zatwierdzone', 'Rejected':'Odrzucone', 'Reason if rejected':'Powód odrzucenia', 'Not interesting enough':'Niewystarczająco ciekawe', 'Reading notes / item text':'Czytanie notatek / tekstu przedmiotu', 'Game dialogue / cutscene':'Dialog z gry / przerywnik', 'Not enough emotion':'Za mało emocji', 'No clear point or punchline':'Brak wyraźnej myśli lub puenty', 'Bad transcription':'Błędna transkrypcja', 'Save status':'Zapisz status', 'Caption text (you can correct the transcription)':'Tekst napisów (możesz poprawić transkrypcję)', 'Save caption text':'Zapisz tekst napisów', 'Mute profanity from mid-word in exported audio and mask it in captions':'Wycisz wulgaryzmy od środka słowa w eksporcie audio i zamaskuj je w napisach', 'Remove long pauses from preview and exported video':'Usuń długie pauzy z podglądu i eksportowanego filmu', 'Export file name (optional)':'Nazwa pliku eksportu (opcjonalnie)', 'Approve before export':'Zatwierdź przed eksportem',
  'Suggested score':'Ocena sugerowana', 'Quality':'Jakość', 'Short potential':'Potencjał shorta', 'Context':'Kontekst', 'Self-contained':'Samowystarczalność', 'Extended completeness':'Pełność rozszerzona', 'These scores explain the selected clip. They do not replace your approval decision.':'Te oceny wyjaśniają wybrany klip. Nie zastępują Twojej decyzji o zatwierdzeniu.', 'Legend':'Legenda',
  'Based on your active discovery profile, prompt/reference similarity, previous approvals and rejections, and relevant reactions from game, voice and chat.':'Bazuje na aktywnym profilu wyszukiwania, podobieństwie do promptów/wzorców, wcześniejszych akceptacjach i odrzuceniach oraz istotnych reakcjach gry, głosu i czatu.', 'Rewards natural speaking pace, usable clip length, emotion or a verified game-to-voice reaction. Reading cues, static text-heavy game screens and incomplete speech lower it.':'Premiuje naturalne tempo wypowiedzi, użyteczną długość klipu, emocję albo potwierdzoną reakcję gra → głos. Obniżają ją sygnały czytania, statyczne ekrany gry z dużą ilością tekstu i urwana wypowiedź.', 'Rewards a short-friendly length, a clear standalone thought, a hook, payoff, emotion and chat engagement. It is strongly reduced by reading aloud, very long clips or missing context.':'Premiuje długość odpowiednią dla shorta, jasną samodzielną myśl, hook, puentę, emocje i zaangażowanie czatu. Jest mocno obniżana przez czytanie na głos, bardzo długie klipy lub brak kontekstu.', 'Compares the surrounding speech before and after the fragment. A score falls when the clip begins or ends in the middle of a thought.':'Porównuje wypowiedź przed i po fragmencie. Ocena spada, gdy klip zaczyna się lub kończy w środku myśli.',
  'Measures whether a new viewer can understand the clip without previous conversation. Complete sentences and an understandable point increase it.':'Sprawdza, czy nowy widz zrozumie klip bez wcześniejszej rozmowy. Pełne zdania i czytelna myśl podnoszą wynik.', 'Available after Extended analysis. It performs an extra check of sentence boundaries, a complete ending and whether the clip reaches its point or punchline.':'Dostępne po analizie rozszerzonej. Dodatkowo sprawdza granice zdań, pełne zakończenie oraz to, czy klip dochodzi do swojej myśli lub puenty.',
  'Use':'Użyj', 'Delete':'Usuń', 'Use collection':'Użyj kolekcji', 'Reimport folder':'Zaimportuj folder ponownie', 'No recordings yet.':'Brak nagrań.', 'No candidates yet. Analysis may still be running.':'Brak kandydatów. Analiza może nadal trwać.', 'No rejected clips with saved reasons yet.':'Brak odrzuconych klipów z zapisanym powodem.', 'No analysed tags yet.':'Brak przeanalizowanych tagów.',
  'Default - no additional patterns':'Domyślny - bez dodatkowych wzorców', 'Imports one public short/video into the selected collection. Use only content you are allowed to download.':'Importuje jeden publiczny Short/film do wybranej kolekcji. Używaj tylko treści, które możesz legalnie pobrać.', 'Streams only temporary audio and one preview frame. The source video is not saved. After review, its analytical fingerprint can be added to a discovery pattern set.':'Pobiera tymczasowo tylko dźwięk i jedną klatkę podglądu. Film źródłowy nie jest zapisywany. Po ocenie jego charakterystyka analityczna może zostać dodana do zestawu wzorców.',
  'No captions':'Bez napisów', 'Clean - outlined':'Czyste - z obrysem', 'Word highlight - large yellow':'Podświetlane słowo - duże żółte', 'Minimal - compact':'Minimalne - kompaktowe', 'Boxed Pop - bold caption card':'Boxed Pop - mocna karta napisów', 'Neon Gaming - colourful gaming glow':'Neon Gaming - kolorowa gamingowa poświata', 'Cinematic - dark subtitle band':'Cinematic - ciemny pas napisów', 'Karaoke Punch - stronger word bounce':'Karaoke Punch - mocniejszy ruch słowa', 'Minimal Center - quiet centered text':'Minimal Center - spokojny tekst na środku', 'Bungee - graffiti/display':'Bungee - graffiti / display', 'Cinzel - old book':'Cinzel - stara książka', 'Pixelify Sans - pixel art':'Pixelify Sans - pixel art', 'Black':'Czarny', 'White':'Biały', 'Custom colour':'Własny kolor', 'These are test captions':'To są napisy testowe', 'Captions are turned off.':'Napisy są wyłączone.', 'The highlighted word shows the active-word style.':'Podświetlone słowo pokazuje styl aktywnego słowa.', 'Captions are disabled for export.':'Napisy są wyłączone dla eksportu.', 'Clean: medium text with a strong outline. The active-word colour is not used by this preset.':'Czyste: średni tekst z mocnym obrysem. Ten preset nie używa koloru aktywnego słowa.', 'Word highlight: the currently spoken word changes colour and becomes larger.':'Podświetlane słowo: aktualnie wypowiadane słowo zmienia kolor i staje się większe.', 'Minimal: smaller, lighter text with a subtle outline. The active-word colour is not used by this preset.':'Minimalne: mniejszy, lżejszy tekst z delikatnym obrysem. Ten preset nie używa koloru aktywnego słowa.', 'Boxed Pop: bold text in a compact caption card.':'Boxed Pop: pogrubiony tekst na kompaktowej karcie napisów.', 'Neon Gaming: gaming-style text; the spoken word is larger and uses the active colour.':'Neon Gaming: tekst w stylu gamingowym; wypowiadane słowo jest większe i używa aktywnego koloru.', 'Cinematic: understated text on a dark subtitle band.':'Cinematic: stonowany tekst na ciemnym pasie napisów.', 'Karaoke Punch: each spoken word uses a stronger bounce and active colour.':'Karaoke Punch: każde wypowiadane słowo ma mocniejszy ruch i aktywny kolor.', 'Minimal Center: quiet, centered text. This preset forces the center position in exported clips.':'Minimal Center: spokojny, wyśrodkowany tekst. Ten preset wymusza środek ekranu w eksportowanych klipach.',
  'Original (default)':'Oryginalny (domyślny)', 'Vertical - calibrated camera center':'Pionowy - kamera na środku', 'Vertical - calibrated gameplay center':'Pionowy - gra na środku', 'Vertical - camera top, gameplay bottom':'Pionowy - kamera u góry, gra na dole', 'Vertical layout preview':'Podgląd pionowego układu', 'Layout name, e.g. facecam top right':'Nazwa układu, np. kamera prawa góra', 'Track 1 - microphone + game (default)':'Ścieżka 1 - mikrofon + gra (domyślnie)', 'Track 1 (default)':'Ścieżka 1 (domyślna)', 'Track 1':'Ścieżka 1', 'Track 2':'Ścieżka 2', 'Track 3':'Ścieżka 3', 'Track 4':'Ścieżka 4', 'Audio track':'Ścieżka audio',
  'Fast - text search and basic tags':'Szybka - wyszukiwanie tekstu i podstawowe tagi', 'Default - current full analysis':'Domyślna - pełna bieżąca analiza', 'Extended - full analysis plus verification of strongest clips':'Rozszerzona - pełna analiza i weryfikacja najlepszych klipów', 'Current workflow: speech, audio/game reaction, visual checks, context, tags and ranking.':'Bieżący proces: mowa, reakcja dźwięku/gry, sprawdzenie obrazu, kontekst, tagi i ranking.', 'YouTube video or Twitch VOD URL':'Link do filmu YouTube lub VOD-a Twitch', 'Preparing upload...':'Przygotowywanie wysyłania...', 'Calculating disk usage...':'Obliczanie zajętego miejsca...',
  '4/5 height':'Wysokość 4/5', '2/5 height':'Wysokość 2/5', 'Caption height preview':'Podgląd wysokości napisów', 'Test captions':'Napisy testowe', 'Select a clip to edit it.':'Wybierz klip do edycji.', 'Hide clip editor':'Ukryj edytor klipu', 'Clip panel tabs':'Zakładki panelu klipu', 'Scoring legend':'Legenda ocen', 'Tag feedback':'Ocena tagów', 'Mark each assigned tag as correct or incorrect. This is stored locally and helps audit future improvements.':'Oznacz każdy przyznany tag jako poprawny lub błędny. Jest to zapisywane lokalnie i pomaga sprawdzać przyszłe ulepszenia.', 'tags reviewed':'tagów przejrzanych', 'Mark a tag as correct when it fits the clip, or incorrect when it was assigned by mistake.':'Oznacz tag jako poprawny, gdy pasuje do klipu, albo jako błędny, gdy został przyznany przez pomyłkę.', 'Correct':'Poprawny', 'Incorrect':'Błędny', 'Clear':'Wyczyść', 'This clip has no assigned tags yet.':'Ten klip nie ma jeszcze przyznanych tagów.', 'Cleared feedback for tag':'Wyczyszczono ocenę tagu', 'Tag marked correct':'Tag oznaczony jako poprawny', 'Tag marked incorrect':'Tag oznaczony jako błędny', 'Listening':'Odsłuchiwanie', 'Stop':'Zatrzymaj', 'Full recording':'Całe nagranie', 'Close':'Zamknij', 'Diagnostic report':'Raport diagnostyczny', 'Contains technical state and recent errors, without recordings or transcripts.':'Zawiera stan techniczny i ostatnie błędy, bez nagrań ani transkrypcji.', 'Copy report':'Kopiuj raport', 'Save .txt file':'Zapisz plik .txt', 'Original recording':'Oryginalne nagranie', 'Previous':'Poprzedni', 'Next':'Następny', 'approve':'zatwierdź', 'reject':'odrzuć',
  'Diagnostic tags':'Tagi diagnostyczne', 'Quality signals':'Sygnały jakości', 'Short signals':'Sygnały potencjału shorta', 'Moment → reaction':'Moment → reakcja', 'Chat reaction':'Reakcja czatu', 'Viewer question match':'Dopasowanie pytania widza', 'Before':'Przed', 'After':'Po', 'no recognised speech in the preceding 12 seconds.':'brak rozpoznanej mowy w poprzednich 12 sekundach.', 'no recognised speech in the following 12 seconds.':'brak rozpoznanej mowy w kolejnych 12 sekundach.', 'No recognized speech':'Brak rozpoznanej mowy', 'No chat transcript imported for this recording yet.':'Dla tego nagrania nie zaimportowano jeszcze transkrypcji czatu.', 'named viewers':'nazwanych widzów', 'Delay':'Opóźnienie',
  'Review statistics':'Statystyki przeglądów', 'All locally analysed recordings. Use this to see what the ranking gets right or wrong.':'Wszystkie lokalnie przeanalizowane nagrania. Sprawdzaj tutaj, co ranking ocenia trafnie, a co wymaga poprawy.', 'Why clips are rejected':'Dlaczego klipy są odrzucane', 'Approved vs rejected scores':'Oceny zaakceptowanych i odrzuconych', 'A large gap means this score helps selection. A small or reversed gap is a signal that it needs tuning.':'Duża różnica oznacza, że ta ocena pomaga w selekcji. Mała lub odwrócona różnica sygnalizuje potrzebę dostrojenia.', 'Tags and decisions':'Tagi i decyzje', 'Shows the most frequent tags and how often clips with that tag are approved or rejected.':'Pokazuje najczęstsze tagi oraz to, jak często klipy z danym tagiem są akceptowane lub odrzucane.', 'Analysis modes and reading filter':'Tryby analizy i filtr czytania', 'Refresh':'Odśwież',
  'messages':'wiadomości', 'Listening to clip':'Odsłuchiwanie klipu', 'Listening to dynamic clip':'Odsłuchiwanie dynamicznego klipu', 'Active collection':'Aktywna kolekcja', 'Active prompt':'Aktywny prompt', 'none':'brak', 'Source recordings':'Nagrania źródłowe', 'exported clips':'wyeksportowane klipy', 'Reanalyze recording':'Przeanalizuj nagranie ponownie', 'Run analysis again':'Uruchom analizę ponownie', 'Delete recording':'Usuń nagranie', 'Analysis queued again.':'Analiza została ponownie dodana do kolejki.', 'Approved':'Zatwierdzone', 'Rejected':'Odrzucone', 'Reviewed':'Przejrzane', 'Approval rate':'Współczynnik akceptacji', 'approved':'zaakceptowane', 'rejected':'odrzucone', 'unreviewed':'nieprzejrzane', 'no decisions':'brak decyzji', 'analysis':'analiza', 'Fast':'Szybka', 'Default':'Domyślna', 'Extended':'Rozszerzona', 'Version':'Wersja', 'Transcription':'Transkrypcja', 'Similarity search':'Wyszukiwanie podobieństw', 'No NVIDIA GPU detected':'Nie wykryto karty NVIDIA', 'Check manually for a newer ClipFinder release.':'Sprawdź ręcznie, czy dostępna jest nowsza wersja ClipFinder.', 'Open release on GitHub':'Otwórz wydanie na GitHubie', 'Software updates':'Aktualizacje programu', 'Update available':'Dostępna aktualizacja', 'Click Check for updates for details.':'Kliknij „Sprawdź aktualizacje”, aby zobaczyć szczegóły.', 'Checking GitHub releases...':'Sprawdzanie wydań na GitHubie...', 'No release notes were provided.':'Nie dodano opisu wydania.', 'is up to date':'jest aktualna', 'latest':'najnowsza', 'Download full installer':'Pobierz pełny instalator', 'Compact update':'Mała aktualizacja', 'Full update':'Pełna aktualizacja', 'Download compact update':'Pobierz małą aktualizację', 'Download the full installer, close ClipFinder, then run it to update in place.':'Pobierz pełny instalator, zamknij ClipFinder, a następnie uruchom instalator, aby zaktualizować aplikację w tym samym miejscu.', 'Preparing update download...':'Przygotowywanie pobierania aktualizacji...', 'Downloading update':'Pobieranie aktualizacji', 'Compact update is ready. ClipFinder will close, verify changed files and reopen.':'Mała aktualizacja jest gotowa. ClipFinder zamknie się, sprawdzi zmienione pliki i uruchomi ponownie.', 'Update is ready. ClipFinder will close, install the update, then reopen.':'Aktualizacja jest gotowa. ClipFinder zamknie się, zainstaluje aktualizację, a następnie uruchomi ponownie.', 'Restart and apply update':'Uruchom ponownie i zastosuj aktualizację', 'Restart and install update':'Uruchom ponownie i zainstaluj aktualizację', 'Update download failed':'Nie udało się pobrać aktualizacji', 'Could not start the update':'Nie udało się uruchomić aktualizacji', 'Closing ClipFinder and installing the update...':'Zamykanie ClipFindera i instalowanie aktualizacji...', 'Could not install the update':'Nie udało się zainstalować aktualizacji', 'Could not check for updates':'Nie udało się sprawdzić aktualizacji',
  'Review audio archive':'Archiwum audio ocenionych klipów', 'Remove source video':'Usuń film źródłowy', 'Source video removed - analysis, tags, reviews and archived audio are retained.':'Film źródłowy usunięty - analiza, tagi, decyzje i zarchiwizowane audio zostały zachowane.', 'The source video was removed.':'Film źródłowy został usunięty.', 'MP4 export unavailable after source removal.':'Eksport MP4 jest niedostępny po usunięciu filmu źródłowego.', 'Archived review audio is still available from Listen.':'Zarchiwizowane audio ocenionego klipu jest nadal dostępne przez Odsłuchaj.', 'Remove the source video':'Usuń film źródłowy', 'This frees disk space but keeps all analysis, tags, transcripts, chat data and review decisions. Audio MP3 files will be kept for every clip with human feedback.':'To zwalnia miejsce na dysku, ale zachowuje analizę, tagi, transkrypcje, dane czatu i decyzje. Plik MP3 zostanie zachowany dla każdego klipu z ręczną oceną, sprawdzonym tagiem, edycją lub użyciem jako wzorzec.', 'Source video removed.':'Film źródłowy został usunięty.', 'Review data and archived audio were kept.':'Dane oceny i zarchiwizowane audio zostały zachowane.',
  'Fast: uses the small speech model with text search and basic tags. It skips scenes, game audio, visual checks and context.':'Szybka: używa małego modelu mowy, wyszukiwania tekstu i podstawowych tagów. Pomija sceny, dźwięk gry, sprawdzenie obrazu i kontekst.', 'Default: current full workflow with speech, audio/game reaction, visual checks, context, tags and ranking.':'Domyślna: pełny bieżący proces z mową, reakcją dźwięku/gry, sprawdzeniem obrazu, kontekstem, tagami i rankingiem.', 'Extended: full workflow plus a 20-second context window, stronger reading detection, hook and ending verification, semantic Q&A matching and stricter duplicate suppression.':'Rozszerzona: pełny proces oraz 20-sekundowe okno kontekstu, silniejsze wykrywanie czytania, weryfikacja hooka i zakończenia, semantyczne dopasowanie pytań i odpowiedzi oraz ostrzejsze usuwanie duplikatów.',
  'good clip length':'dobra długość klipu', 'long clip':'długi klip', 'natural speaking pace':'naturalne tempo wypowiedzi', 'emotion or opinion':'emocja lub opinia', 'expressive delivery':'ekspresyjna wypowiedź', 'possible reading aloud':'możliwe czytanie na głos', 'some reading cues':'częściowe sygnały czytania', 'game sound followed by microphone reaction':'dźwięk gry, po którym nastąpiła reakcja mikrofonem', 'expressive microphone delivery':'ekspresyjna reakcja mikrofonem', 'expressive vocal delivery':'ekspresyjny sposób mówienia', 'monotonous vocal delivery':'jednostajny sposób mówienia', 'static text-heavy game screen':'statyczny ekran gry z dużą ilością tekstu', 'extended document/task reading verification':'rozszerzona weryfikacja czytania dokumentu/zadania', 'extended reading cues':'rozszerzone sygnały czytania', 'visual action':'akcja wizualna', 'extended complete-thought verification':'rozszerzona weryfikacja pełnej myśli', 'extended incomplete-thought warning':'ostrzeżenie o urwanej myśli', 'clear opening hook':'wyraźny hook na początku', 'resolved ending or payoff':'domknięte zakończenie lub puenta', 'weak opening depends on earlier speech':'słaby początek zależny od wcześniejszej wypowiedzi', 'ending does not resolve the thought':'zakończenie nie domyka myśli', 'start aligned to sentence':'początek dopasowany do zdania', 'end aligned to sentence':'koniec dopasowany do zdania', 'extended to punchline':'rozszerzono do puenty',
  'approved':'zaakceptowane', 'rejected':'odrzucone', 'unreviewed':'nieprzejrzane', 'Possible reading':'Możliwe czytanie', 'ready':'gotowe', 'processing':'analizowanie', 'paused':'wstrzymane', 'queued':'w kolejce', 'running':'w toku', 'cancelled':'anulowane', 'failed':'błąd', 'interrupted':'przerwane', 'Pause analysis':'Wstrzymaj analizę', 'Pause requested':'Trwa wstrzymywanie', 'Resume analysis':'Wznów analizę', 'Cancel analysis':'Anuluj analizę', 'Cancel import':'Anuluj import',
  'e.g. giveaway, important announcement':'np. rozdanie, ważne ogłoszenie', 'Name, e.g. funny reactions':'Nazwa, np. śmieszne reakcje', 'Description, e.g. funny and emotional reactions to unexpected events':'Opis, np. śmieszne i emocjonalne reakcje na nieoczekiwane wydarzenia', 'Choose a saved prompt or write a temporary description':'Wybierz zapisany prompt lub wpisz tymczasowy opis', 'e.g. best reactions':'np. najlepsze reakcje', 'No candidates match the active filters. Clear the text, tag or status filter to see all analysed clips.':'Żadne klipy nie pasują do aktywnych filtrów. Wyczyść filtr tekstu, tagu lub statusu, aby zobaczyć wszystkie przeanalizowane klipy.', 'No candidates match the active prompt or reference collection.':'Żadne klipy nie pasują do aktywnego promptu ani kolekcji wzorców.',
  'Learning data for this profile':'Dane uczące dla tego profilu', 'analysed Shorts':'przeanalizowanych Shortów', 'Default mode':'Tryb domyślny', 'no additional Short patterns':'brak dodatkowych wzorców Shortów', 'General - best mixed clips':'Ogólny - najlepsze różnorodne klipy', 'Game quote/event -> your reaction':'Cytat/wydarzenie z gry → Twoja reakcja',
  'self-contained':'samowystarczalny', 'short-friendly length':'długość odpowiednia dla shorta', 'usable short length':'użyteczna długość shorta', 'too long for a short':'za długi na shorta', 'long for a short':'długi jak na shorta', 'very brief clip':'bardzo krótki klip', 'stands on its own':'jest zrozumiały bez kontekstu', 'mostly self-contained':'w większości samowystarczalny', 'needs surrounding context':'wymaga otaczającego kontekstu', 'complete thought':'pełna myśl', 'unclear thought':'niejasna myśl', 'verified complete ending':'zweryfikowane pełne zakończenie', 'incomplete ending':'niepełne zakończenie', 'clear content hook':'wyraźny hook treści', 'answer with context':'odpowiedź z kontekstem', 'game moment to reaction':'moment w grze → reakcja', 'expressive voice':'ekspresyjny głos', 'chat reacted':'reakcja czatu', 'chat amusement':'rozbawienie czatu', 'not enough spoken content':'za mało wypowiedzianej treści', 'too much spoken content':'za dużo wypowiedzianej treści', 'likely reading aloud':'prawdopodobne czytanie na głos', 'reading cues':'sygnały czytania', 'exceptional short criteria met':'spełnia kryteria wyjątkowego shorta', 'exceptional quality criteria met':'spełnia kryteria wyjątkowej jakości', 'game -> voice':'gra → głos', 'game -> voice -> chat':'gra → głos → czat'
};
const originalLocalizedText = new WeakMap();
const originalLocalizedAttributes = new WeakMap();
function translateForLanguage(value, language = state.interfaceLanguage) { const text = String(value ?? ''); return language === 'pl' ? (polishText[text] || text) : text; }
function t(value) { return translateForLanguage(value); }
function localizedNodeValue(original, language) {
  const trimmed = original.trim(); const translated = translateForLanguage(trimmed, language);
  if (!trimmed || translated === trimmed) return original;
  const leading = original.match(/^\s*/)?.[0] || ''; const trailing = original.match(/\s*$/)?.[0] || '';
  return `${leading}${translated}${trailing}`;
}
function localizeTextNode(node, displayedLanguage = state.interfaceLanguage) {
  if (!node || node.nodeType !== Node.TEXT_NODE) return;
  if (!originalLocalizedText.has(node)) originalLocalizedText.set(node, node.nodeValue || '');
  let original = originalLocalizedText.get(node) || '';
  if (node.nodeValue !== localizedNodeValue(original, displayedLanguage)) {
    original = node.nodeValue || ''; originalLocalizedText.set(node, original);
  }
  node.nodeValue = localizedNodeValue(original, state.interfaceLanguage);
}
function localizeAttribute(element, attribute, displayedLanguage = state.interfaceLanguage) {
  if (!element?.hasAttribute(attribute)) return;
  let values = originalLocalizedAttributes.get(element);
  if (!values) { values = new Map(); originalLocalizedAttributes.set(element, values); }
  const current = element.getAttribute(attribute) || '';
  if (!values.has(attribute)) values.set(attribute, current);
  let original = values.get(attribute) || '';
  if (current !== localizedNodeValue(original, displayedLanguage)) {
    original = current; values.set(attribute, original);
  }
  element.setAttribute(attribute, localizedNodeValue(original, state.interfaceLanguage));
}
function localizeTree(root = document.body, displayedLanguage = state.interfaceLanguage) {
  if (!root) return;
  if (root.nodeType === Node.TEXT_NODE) { localizeTextNode(root, displayedLanguage); return; }
  if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = []; while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach((node) => localizeTextNode(node, displayedLanguage));
  const elements = root.nodeType === Node.ELEMENT_NODE ? [root, ...root.querySelectorAll('*')] : [...document.querySelectorAll('*')];
  elements.forEach((element) => ['placeholder', 'aria-label', 'title'].forEach((attribute) => localizeAttribute(element, attribute, displayedLanguage)));
}
const interfaceText = {
  en: {
    title:'ClipFinder Local', savedSetup:'Saved setup', savedSetupHeading:'Saved setup', savedSetupHint:'Search tools and global settings - available at any time.', hide:'Hide', search:'Search', global:'Global', statistics:'Statistics', options:'Options',
    upload:'New recording', recordings:'Recordings', refresh:'Refresh now', languageTitle:'Application language', languageHint:'Changes the ClipFinder interface only. Analysis models, tags and saved data stay unchanged.', languageLabel:'Interface language',
    updates:'Software updates', checkUpdates:'Check for updates', downloadUpdate:'Download update', manualDownload:'Download manually', diagnostics:'Open diagnostic report'
  },
  pl: {
    title:'ClipFinder Local', savedSetup:'Zapisane ustawienia', savedSetupHeading:'Zapisane ustawienia', savedSetupHint:'Narzędzia wyszukiwania, ustawienia globalne i opcje aplikacji.', hide:'Ukryj', search:'Wyszukiwanie', global:'Globalne', statistics:'Statystyki', options:'Opcje',
    upload:'Nowe nagranie', recordings:'Nagrania', refresh:'Odśwież', languageTitle:'Język aplikacji', languageHint:'Zmienia tylko interfejs ClipFindera. Modele analizy, tagi i zapisane dane pozostają bez zmian.', languageLabel:'Język interfejsu',
    updates:'Aktualizacje programu', checkUpdates:'Sprawdź aktualizacje', downloadUpdate:'Pobierz aktualizację', manualDownload:'Pobierz ręcznie', diagnostics:'Otwórz raport diagnostyczny'
  }
};
function setInterfaceLanguage(value, persist = true) {
  const previousLanguage = state.interfaceLanguage || 'en';
  const language = value === 'pl' ? 'pl' : 'en'; const text = interfaceText[language];
  state.interfaceLanguage = language; document.documentElement.lang = language; document.title = text.title;
  const select = $('#application-language'); if (select) select.value = language;
  if (persist) try { localStorage.setItem(APP_LANGUAGE_KEY, language); } catch { /* Optional local preference only. */ }
  localizeTree(document.body, previousLanguage);
  if ($('#selection-summary')) updateSelectionSummary();
  if (state.editingSegment) { renderDetailedScoring(state.editingSegment); renderTagFeedback(state.editingSegment); }
  if ($('#discovery-profile') && state.discovery?.profiles?.length) {
    const profileSelect = $('#discovery-profile');
    for (const profile of state.discovery.profiles) {
      const option = [...profileSelect.options].find((item) => item.value === profile.id);
      if (option) option.textContent = `${t(profile.name)} (${profile.accepted || 0} ${t('approved')} / ${profile.rejected || 0} ${t('rejected')})`;
    }
    renderDiscoveryPatternSets();
    updateDiscoveryPatternFeedback();
  }
  if (document.querySelector('[data-setup-panel="statistics"]')?.classList.contains('active')) void loadStatistics();
}
const GLOBAL_SESSION_KEY = 'clipfinder-global-session-v1';
function readGlobalSession() {
  try { const value = JSON.parse(localStorage.getItem(GLOBAL_SESSION_KEY) || '{}'); return value && typeof value === 'object' ? value : {}; }
  catch { return {}; }
}
state.globalSession = readGlobalSession();
function rememberGlobalSession() {
  try {
    state.globalSession = {
      caption: state.globalCaption,
      export: state.globalExport,
      analysisAudio: state.analysisAudio,
      discoveryProfile: $('#discovery-profile')?.value || state.globalSession.discoveryProfile || state.discovery.active_profile || 'general',
      discoveryPatternSet: $('#discovery-pattern-set')?.value || state.globalSession.discoveryPatternSet || state.discovery.pattern_set_id || '',
      discoveryProfanityFilter: $('#discovery-profanity-filter')?.value || state.globalSession.discoveryProfanityFilter || state.discovery.profanity_filter || 'allow',
    };
    localStorage.setItem(GLOBAL_SESSION_KEY, JSON.stringify(state.globalSession));
  } catch { /* Local session restore is optional. */ }
}
function setSavedListCount(selector, count) { const node = $(selector); if (node) node.textContent = String(count); }
const fmt = (seconds) => new Date(seconds * 1000).toISOString().slice(11, 19);
const elapsed = (seconds) => {
  const total = Math.max(0, Math.round(Number(seconds || 0)));
  const hours = Math.floor(total / 3600); const minutes = Math.floor((total % 3600) / 60); const rest = total % 60;
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
};
const clamp = (number) => Math.max(0, Math.min(100, Number(number || 0)));
const bytes = (value) => { const amount = Number(value || 0); if (!amount) return '0 B'; const units = ['B', 'KB', 'MB', 'GB', 'TB']; const index = Math.min(units.length - 1, Math.floor(Math.log(amount) / Math.log(1024))); return `${(amount / (1024 ** index)).toFixed(index < 2 ? 0 : 1)} ${units[index]}`; };

document.addEventListener('pointerdown', (event) => {
  const button = event.target.closest('button, .quiet');
  if (!button || button.disabled) return;
  button.classList.remove('button-pop');
  requestAnimationFrame(() => button.classList.add('button-pop'));
  window.setTimeout(() => button.classList.remove('button-pop'), 320);
  if (button.closest('.actions')) {
    button.classList.remove('action-clicked');
    void button.offsetWidth;
    button.classList.add('action-clicked');
    window.setTimeout(() => button.classList.remove('action-clicked'), 460);
  }
});

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, { cache: 'no-store', ...options });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || 'Request failed.');
  const type = response.headers.get('content-type') || '';
  return type.includes('application/json') ? response.json() : response;
}
function uploadVideo(file, analysisMode) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    const data = new FormData();
    data.append('file', file);
    data.append('analysis_mode', analysisMode);
    request.open('POST', '/api/videos');
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) setUploadProgress(event.loaded / event.total * 100, `Uploading ${file.name}: ${Math.round(event.loaded / event.total * 100)}%`);
      else setUploadProgress(0, `Uploading ${file.name}...`);
    };
    request.onerror = () => reject(new Error('The browser could not reach the local upload endpoint.'));
    request.onload = () => {
      let body = {};
      try { body = JSON.parse(request.responseText || '{}'); } catch { /* handled below */ }
      if (request.status >= 200 && request.status < 300) resolve(body);
      else reject(new Error(body.detail || `Upload failed (HTTP ${request.status}).`));
    };
    request.send(data);
  });
}
function message(text, error = false) { if (error) state.statusErrorUntil = Date.now() + 12000; const el = $('#status'); el.textContent = text; el.style.color = error ? 'var(--danger)' : 'var(--muted)'; }
function analysisModeDescription() {
  const mode = $('#analysis-mode').value;
  const descriptions = {
    fast: 'Fast: uses the small speech model with text search and basic tags. It skips scenes, game audio, visual checks and context.',
    default: 'Default: current full workflow with speech, audio/game reaction, visual checks, context, tags and ranking.',
    extended: 'Extended: full workflow plus a 20-second context window, stronger reading detection, hook and ending verification, semantic Q&A matching and stricter duplicate suppression.'
  };
  $('#analysis-mode-description').textContent = t(descriptions[mode] || descriptions.default);
}
async function loadRuntimeStatus() {
  if (state.runtimeStatusPromise) return state.runtimeStatusPromise;
  const request = (async () => {
    const runtime = await api('/runtime-status');
    state.appVersion = String(runtime.version || 'unknown');
    $('#runtime-headline').textContent = runtime.headline;
    $('#app-version').textContent = `${t('Version')} ${runtime.version || '--'}`;
    const gpu = runtime.gpu ? `${runtime.gpu.name} / ${runtime.gpu.memory_mb} MB VRAM` : t('No NVIDIA GPU detected');
    $('#runtime-detail').textContent = `${t('Transcription')}: ${runtime.transcription.label}. ${t('Similarity search')}: ${runtime.embeddings.label}. ${gpu}.`;
    $('#runtime-headline').classList.toggle('runtime-warning', runtime.transcription.mode === 'unavailable');
    return runtime;
  })();
  state.runtimeStatusPromise = request;
  try { return await request; }
  finally { if (state.runtimeStatusPromise === request) state.runtimeStatusPromise = null; }
}
function startupUpdateStorageKey(version = state.appVersion) { return `clipfinder-update-available:${version || 'unknown'}`; }
function showStartupUpdateNotice(latestVersion) {
  const notice = $('#startup-update-notice');
  notice.textContent = latestVersion ? `Update available: ${latestVersion}` : 'Update available';
  notice.hidden = false;
}
function rememberStartupUpdate(update) {
  const currentVersion = String(update.current_version || state.appVersion || 'unknown');
  state.appVersion = currentVersion;
  if (update.update_available && update.latest_version) {
    try { localStorage.setItem(startupUpdateStorageKey(currentVersion), String(update.latest_version)); } catch { /* Optional local notice only. */ }
    showStartupUpdateNotice(update.latest_version);
  } else if (!update.update_available) {
    try { localStorage.removeItem(startupUpdateStorageKey(currentVersion)); } catch { /* Optional local notice only. */ }
    $('#startup-update-notice').hidden = true;
  }
}
async function checkForStartupUpdate() {
  const currentVersion = state.appVersion;
  if (!currentVersion) return;
  try {
    const knownVersion = localStorage.getItem(startupUpdateStorageKey(currentVersion));
    if (knownVersion) { showStartupUpdateNotice(knownVersion); return; }
  } catch { /* Local storage can be unavailable in an embedded browser. */ }
  try {
    const update = await api('/update-status');
    if (!update.error) rememberStartupUpdate(update);
  } catch { /* A startup update check must never interrupt ClipFinder. */ }
}
function openStartupUpdateNotice() {
  setSetupSidebar(true);
  setSetupTab('options');
  let knownVersion = '';
  try { knownVersion = localStorage.getItem(startupUpdateStorageKey()) || ''; } catch { /* Optional local notice only. */ }
  $('#update-status').textContent = knownVersion
    ? `${t('Update available')}: ${knownVersion}. ${t('Click Check for updates for details.')}`
    : `${t('Update available')}. ${t('Click Check for updates for details.')}`;
  requestAnimationFrame(() => document.querySelector('.updates-card')?.scrollIntoView({ behavior:'smooth', block:'start' }));
}
function setUploadProgress(percent, label, error = false) {
  const block = $('#upload-progress'); block.hidden = false; block.classList.toggle('error', error);
  $('#upload-progress-label').textContent = label;
  $('#upload-progress-fill').style.width = `${clamp(percent)}%`;
}
function make(tag, className, text = '') { const el = document.createElement(tag); if (className) el.className = className; if (text) el.textContent = text; return el; }
const diagnosticTagPrefixes = ['reakcja: ', 'kontekst: ', 'struktura: ', 'format: ', 'moment: ', 'wypowiedź: '];
function isDiagnosticTag(tag) {
  const value = String(tag || '');
  return value === 'reading' || diagnosticTagPrefixes.some((prefix) => value.startsWith(prefix));
}
function contentTagCategory(tag) {
  const value = String(tag || '');
  if (value.startsWith('emocja:') || ['radość', 'złość', 'gniew', 'smutek', 'zaskoczenie'].includes(value)) return 'emotion';
  if (value === 'humor') return 'humour';
  if (value.startsWith('forma:') || ['wyrażanie opinii', 'rekomendacja'].includes(value)) return 'speech_form';
  if (value === 'reakcja na grę') return 'game_reaction';
  if (value === 'pytanie' || value === 'odpowiedź na pytanie widza') return 'viewer_question';
  return `tag:${value}`;
}
function contentTags(tags) {
  const seen = new Set();
  return (tags || []).filter((tag) => {
    if (isDiagnosticTag(tag)) return false;
    const category = contentTagCategory(tag);
    if (seen.has(category)) return false;
    seen.add(category);
    return true;
  });
}
function diagnosticTags(tags) {
  const values = (tags || []).filter(isDiagnosticTag).filter((tag) => tag !== 'reading');
  return [...new Set(values)];
}
function exportLayoutQuery(output) {
  const fields = ['camera_x', 'camera_y', 'camera_width', 'camera_height', 'game_x', 'game_y', 'game_width', 'game_height'];
  return fields.map((field) => `&${field}=${encodeURIComponent(String(Number(output[field])))}`).join('');
}
function makeTagPill(tag, feedback = {}) {
  const pill = make('span', 'tag', tag);
  pill.dataset.tag = tag;
  const verdict = feedback[tag];
  if (verdict === 'correct') pill.classList.add('tag-reviewed-correct');
  if (verdict === 'incorrect') pill.classList.add('tag-reviewed-incorrect');
  return pill;
}
function updateVisibleTagFeedback(segmentId, feedback = {}) {
  document.querySelectorAll('.segment[data-segment-id]').forEach((card) => {
    if (card.dataset.segmentId !== segmentId) return;
    card.querySelectorAll('.tags .tag[data-tag]').forEach((pill) => {
      pill.classList.toggle('tag-reviewed-correct', feedback[pill.dataset.tag] === 'correct');
      pill.classList.toggle('tag-reviewed-incorrect', feedback[pill.dataset.tag] === 'incorrect');
    });
  });
}
function updateSelectionSummary() {
  const prompt = $('#active-prompt').value.trim(); const collection = state.collectionName || 'none';
  $('#selection-summary').textContent = `${t('Active collection')}: ${collection}. ${t('Active prompt')}: ${prompt || t('none')}.`;
  $('#similar-button').disabled = !state.collectionId;
  $('#import-folder-button').disabled = !state.collectionId;
  $('#import-url-button').disabled = !$('#reference-url-collection').value;
  $('#generate-prompt-button').disabled = !state.collectionId;
}
function openSegmentInRecording(segment) {
  if (segment.source_removed) { message(`${t('The source video was removed.')} ${t('Archived review audio is still available from Listen.')}`, true); return; }
  const dialog = $('#video-dialog'); const player = $('#full-video');
  $('#dialog-title').textContent = `Full recording: ${fmt(segment.start_seconds)} - ${fmt(segment.end_seconds)}`;
  $('#dialog-transcript').textContent = segment.transcript || '';
  player.src = `/api/videos/${segment.video_id}/stream#t=${segment.start_seconds.toFixed(2)},${segment.end_seconds.toFixed(2)}`;
  player.onloadedmetadata = () => { player.currentTime = segment.start_seconds; };
  if (!dialog.open) dialog.showModal();
}

function clearFullRecordingPreview() {
  const player = $('#full-video');
  player.pause();
  player.onloadedmetadata = null;
  player.removeAttribute('src');
  player.load();
}

function setClipEditorOpen(open) {
  state.clipEditorOpen = open;
  document.body.classList.toggle('clip-editor-closed', !open);
  const sidebar = $('#clip-editor-sidebar');
  sidebar.setAttribute('aria-hidden', String(!open));
  sidebar.inert = !open;
  $('#clip-editor-toggle').hidden = open;
  $('#clip-editor-toggle').setAttribute('aria-expanded', String(open));
}

function renderStorageSummary(storage) {
  if (!storage) return;
  $('#storage-summary').textContent = `${t('Source recordings')}: ${bytes(storage.video_bytes)} (${storage.video_count}) / ${t('exported clips')}: ${bytes(storage.clip_bytes)} (${storage.clip_count}) / ${t('Review audio archive')}: ${bytes(storage.review_audio_bytes)} (${storage.review_audio_count})`;
}

function invalidateStorageSummary() { state.storageLoadedAt = 0; }

async function loadVideos(forceStorage = false) {
  const requestGeneration = ++state.videoRequestGeneration;
  const shouldLoadStorage = forceStorage || !state.storage || Date.now() - state.storageLoadedAt >= 60000;
  const storageRequest = shouldLoadStorage
    ? api('/storage').then((storage) => ({ storage })).catch((error) => ({ error }))
    : null;
  const videos = await api('/videos');
  if (requestGeneration !== state.videoRequestGeneration) return;
  state.videos = videos;
  state.hasActiveVideoJobs = videos.some((video) => ['queued', 'processing'].includes(video.status));
  const renderSignature = JSON.stringify({ selectedVideoId:state.videoId, videos });
  if (renderSignature !== state.videoRenderSignature) {
    state.videoRenderSignature = renderSignature;
    const box = $('#videos'); box.replaceChildren();
  if (!videos.length) box.append(make('p', 'hint', 'No recordings yet.'));
  else for (const video of videos) {
    const card = make('article', `video ${video.status} ${state.videoId === video.id ? 'selected' : ''}`);
    const info = make('div', 'video-info'); info.append(make('strong', 'video-name', video.original_name));
    const analysisTime = Number(video.analysis_seconds || 0) > 0 ? ` / analysis: ${elapsed(video.analysis_seconds)}` : '';
    const estimate = Number(video.estimated_analysis_seconds || 0) > 0 ? ` / estimated analysis: ~${elapsed(video.estimated_analysis_seconds)} (based on ${video.estimate_sample_count} previous)` : '';
    const analysisMode = t({ fast:'Fast', default:'Default', extended:'Extended' }[video.analysis_mode] || 'Default');
    const retained = video.source_removed ? ` / ${t('Source video removed - analysis, tags, reviews and archived audio are retained.')}` : '';
    const sourceSize = video.source_removed ? bytes(video.source_size_bytes) : bytes(video.size_bytes);
    info.append(make('p', 'video-meta', `${video.duration_seconds ? fmt(video.duration_seconds) : '--:--:--'} / ${sourceSize} / ${analysisMode}${analysisTime}${estimate} / ${video.message || video.status}${retained}`));
    const progress = make('div', 'video-progress'); const track = make('div', 'progress-track'); const fill = make('div', 'progress-fill'); fill.style.width = `${clamp(video.progress)}%`; track.append(fill); progress.append(track, make('strong', '', `${clamp(video.progress)}%`));
    card.append(info, make('span', 'pill', video.status), progress);
    if (['queued', 'processing'].includes(video.status) && video.job_id) {
      const pause = make('button', 'quiet', video.pause_requested ? 'Pause requested' : 'Pause analysis');
      pause.disabled = Boolean(video.pause_requested);
      pause.onclick = async (event) => {
        event.stopPropagation();
        pause.disabled = true;
        try {
          await api(`/jobs/${video.job_id}/pause`, { method:'POST' });
          message(t('Pause requested. The current safe step will finish before the analysis stops.'));
          await refreshDashboard();
        } catch (error) { message(error.message, true); pause.disabled = false; }
      };
      const cancel = make('button', 'quiet danger-button', 'Cancel analysis');
      cancel.onclick = async (event) => {
        event.stopPropagation();
        cancel.disabled = true;
        try {
          await api(`/jobs/${video.job_id}/cancel`, { method:'POST' });
          await refreshDashboard();
        } catch (error) { message(error.message, true); cancel.disabled = false; }
      };
      card.append(pause, cancel);
    }
    if (video.status === 'paused' && video.job_id) {
      const resume = make('button', 'quiet primary-button', 'Resume analysis');
      resume.onclick = async (event) => {
        event.stopPropagation();
        resume.disabled = true;
        try {
          await api(`/jobs/${video.job_id}/resume`, { method:'POST' });
          message(t('Analysis queued for resume.'));
          await refreshDashboard();
        } catch (error) { message(error.message, true); resume.disabled = false; }
      };
      card.append(resume);
    }
    if (['failed', 'interrupted', 'ready'].includes(video.status) && !video.source_removed) {
      const label = video.status === 'ready' ? 'Reanalyze recording' : 'Run analysis again';
      const retry = make('button', 'quiet', label);
      retry.onclick = async (event) => {
        event.stopPropagation();
        retry.disabled = true;
        try {
          await api(`/videos/${video.id}/analyse`, { method:'POST' });
          if (state.videoId === video.id) {
            // A reanalysis replaces segment IDs. Do not retain a prompt,
            // collection, tag or review filter that points at the old set.
            state.resultRequestGeneration += 1;
            state.loadedReadyVideoId = null;
            state.resultMode = 'all';
            state.activeResults = null;
            $('#search').value = '';
            $('#tag-search').value = '';
            $('#rating-search').value = '';
            $('#selected-title').textContent = `${t('Candidates')}: ${video.original_name}`;
          }
          message(t('Analysis queued again.'));
          await refreshDashboard();
        } catch (error) {
          message(error.message, true);
          retry.disabled = false;
        }
      };
      const remove = make('button', 'quiet danger-button', 'Remove source video');
      remove.onclick = async (event) => {
        event.stopPropagation();
        const confirmation = `${t('Remove the source video')} “${video.original_name}” (${bytes(video.size_bytes)})?\n\n${t('This frees disk space but keeps all analysis, tags, transcripts, chat data and review decisions. Audio MP3 files will be kept for every clip with human feedback.')}`;
        if (!window.confirm(confirmation)) return;
        remove.disabled = true;
        try {
          const result = await api(`/videos/${video.id}`, { method:'DELETE' });
          invalidateStorageSummary();
          message(`${t('Source video removed.')} ${t('Review data and archived audio were kept.')} (${result.archived_segments || 0} MP3)`);
          await refreshDashboard();
          if (state.videoId === video.id) await loadSegments();
        } catch (error) { message(error.message, true); remove.disabled = false; }
      };
      card.append(retry, remove);
    }
    card.onclick = () => selectVideo(video); box.append(card);
  }
  }
  if (storageRequest) {
    const storageResult = await storageRequest;
    if (requestGeneration !== state.videoRequestGeneration) return;
    if (storageResult.storage) {
      state.storage = storageResult.storage;
      state.storageLoadedAt = Date.now();
      renderStorageSummary(state.storage);
    } else if (!state.storage) $('#storage-summary').textContent = `${t('Disk usage unavailable')}: ${storageResult.error?.message || t('Unknown error')}`;
  } else renderStorageSummary(state.storage);
}

async function selectVideo(video) {
  state.resultRequestGeneration += 1; state.quickReviewRequestGeneration += 1;
  state.videoId = video.id; state.resultMode = 'all'; state.activeResults = null; state.loadedReadyVideoId = video.status === 'ready' ? video.id : null; state.captionPositions = {}; state.exportNames = {}; clearClipEditor(); $('#workspace').hidden = false; $('#selected-title').textContent = `Candidates: ${video.original_name}`;
  updateSelectionSummary();
  try { await Promise.all([loadVideos(), loadSegments(), loadChatSummary()]); }
  catch (error) { message(error.message, true); }
}

function addRejectionReasons(select) {
  select.querySelectorAll('[data-custom-reason]').forEach((option) => option.remove());
  for (const reason of state.rejectionReasons) {
    if ([...select.options].some((option) => option.value === reason)) continue;
    const option = document.createElement('option'); option.value = reason; option.textContent = reason; select.append(option);
    option.dataset.customReason = 'true';
  }
}

function clearClipEditor() {
  state.editingSegment = null;
  $('#clip-editor-title').textContent = 'Select a clip to edit it.';
  $('#clip-editor-empty').hidden = false;
  $('#clip-editor-form').hidden = true;
}

function setEditorTab(tab) {
  state.editorTab = tab === 'analysis' ? 'analysis' : 'edit';
  document.querySelectorAll('[data-editor-tab]').forEach((button) => {
    const active = button.dataset.editorTab === state.editorTab;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
  });
  $('#clip-editor-edit-tab').hidden = state.editorTab !== 'edit';
  $('#clip-editor-analysis-tab').hidden = state.editorTab !== 'analysis';
}

function updateSegmentFromResponse(segment, updated) {
  Object.assign(segment, updated);
  const original = state.activeResults?.find((item) => item.id === segment.id);
  if (original) Object.assign(original, updated);
}

function renderTagFeedback(segment) {
  const box = $('#editor-tag-feedback-list'); box.replaceChildren();
  const tags = [...new Set((segment.tags || []).filter((tag) => tag !== 'reading'))];
  const feedback = segment.tag_feedback || {};
  const marked = tags.filter((tag) => feedback[tag] === 'correct' || feedback[tag] === 'incorrect').length;
  $('#editor-analysis-summary').textContent = tags.length
    ? `${marked}/${tags.length} ${t('tags reviewed')}. ${t('Mark a tag as correct when it fits the clip, or incorrect when it was assigned by mistake.')}`
    : t('This clip has no assigned tags yet.');
  if (!tags.length) return;
  for (const tag of tags) {
    const row = make('div', 'tag-feedback-row');
    row.append(make('span', 'tag', tag));
    const actions = make('div', 'tag-feedback-actions');
    const verdict = feedback[tag] || 'unmarked';
    for (const [value, label, className] of [['correct', 'Correct', 'correct'], ['incorrect', 'Incorrect', 'incorrect'], ['unmarked', 'Clear', 'clear']]) {
      const button = make('button', `quiet tag-feedback-button ${className}`, t(label));
      button.classList.toggle('selected', verdict === value);
      button.disabled = verdict === value;
      button.onclick = async () => {
        actions.querySelectorAll('button').forEach((item) => { item.disabled = true; });
        try {
          const updated = await api(`/segments/${segment.id}/tag-feedback`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({tag, verdict:value}) });
          updateSegmentFromResponse(segment, updated);
          updateVisibleTagFeedback(segment.id, updated.tag_feedback);
          renderTagFeedback(segment);
          const feedbackMessage = value === 'unmarked'
            ? `${t('Cleared feedback for tag')}: ${tag}`
            : `${t(`Tag marked ${value}`)}: ${tag}`;
          message(feedbackMessage);
        } catch (error) {
          renderTagFeedback(segment);
          message(error.message, true);
        }
      };
      actions.append(button);
    }
    row.append(actions); box.append(row);
  }
}

function renderCardScoreGrid(node, segment) {
  const grid = node.querySelector('.segment-score-grid'); grid.replaceChildren();
  const values = [
    ['Suggested score', segment.ranking_score], ['Quality', segment.quality_score], ['Short potential', segment.short_potential_score],
    ['Context', segment.context_score], ['Self-contained', segment.self_contained_score], ['Extended completeness', segment.extended_completeness_score],
  ];
  for (const [label, raw] of values) {
    const value = Number(raw); const available = Number.isFinite(value) && value >= 0;
    const item = make('div', 'segment-score-item');
    item.append(make('span', 'segment-score-label', label), make('strong', 'segment-score-value', available ? String(Math.round(value)) : '—'));
    grid.append(item);
  }
}

function renderDetailedScoring(segment) {
  const grid = document.querySelector('#editor-score-grid'); grid?.replaceChildren();
  const values = [
    ['Suggested score', segment.ranking_score, '99'],
    ['Quality', segment.quality_score, '99'],
    ['Short potential', segment.short_potential_score, '99'],
    ['Context', segment.context_score, '99'],
    ['Self-contained', segment.self_contained_score, '99'],
    ['Extended completeness', segment.extended_completeness_score, '99'],
  ];
  for (const [label, raw, max] of values) {
    const value = Number(raw);
    const available = Number.isFinite(value) && value >= 0;
    const card = make('div', 'score-card');
    card.append(make('span', 'score-card-label', label), make('strong', 'score-card-value', available ? `${Math.round(value)}/${max}` : '—'));
    grid?.append(card);
  }
  const signals = [];
  const diagnostic = diagnosticTags(segment.tags);
  if (diagnostic.length) signals.push(`${t('Diagnostic tags')}: ${diagnostic.join(', ')}`);
  if ((segment.quality_signals || []).length) signals.push(`${t('Quality signals')}: ${(segment.quality_signals || []).map((item) => t(item)).join(', ')}`);
  if ((segment.short_potential_signals || []).length) signals.push(`${t('Short signals')}: ${(segment.short_potential_signals || []).map((item) => t(item)).join(', ')}`);
  const moment = Number(segment.moment_reaction_score || 0); const chat = Number(segment.chat_reaction_score || 0);
  if (moment) signals.push(`${t('Moment → reaction')}: ${moment}/30${segment.moment_reaction_stage ? ` (${t(segment.moment_reaction_stage)})` : ''}`);
  if (chat) signals.push(`${t('Chat reaction')}: ${chat}/20 / ${Number(segment.chat_message_count || 0)} ${t('messages')}`);
  if (Number(segment.chat_question_match_score || 0) >= 40) signals.push(`${t('Viewer question match')}: ${Math.round(Number(segment.chat_question_match_score))}/99`);
  const box = $('#editor-score-signals'); box.replaceChildren();
  if (!signals.length) { box.hidden = true; return; }
  signals.forEach((text) => box.append(make('p', '', text)));
  box.hidden = false;
}

function selectClipForEditor(segment, openPanel = true) {
  if (openPanel) setClipEditorOpen(true);
  state.editingSegment = segment;
  $('#clip-editor-title').textContent = `${fmt(segment.start_seconds)} - ${fmt(segment.end_seconds)}`;
  $('#clip-editor-empty').hidden = true;
  $('#clip-editor-form').hidden = false;
  $('#editor-start').value = Number(segment.start_seconds).toFixed(1);
  $('#editor-end').value = Number(segment.end_seconds).toFixed(1);
  $('#editor-caption-position').value = state.captionPositions[segment.id] || 'bottom';
  renderEditorCaptionPositionPreview($('#editor-caption-position').value);
  $('#editor-rating-select').value = segment.rating || 'unrated';
  const reviewReason = $('#editor-review-reason'); addRejectionReasons(reviewReason);
  if ([...reviewReason.options].some((option) => option.value === segment.review_reason)) reviewReason.value = segment.review_reason;
  $('#editor-transcript').value = segment.transcript || '';
  $('#editor-censor-profanity').checked = Boolean(segment.censor_profanity);
  const pauseToggle = $('#editor-remove-pauses');
  pauseToggle.checked = Boolean(segment.remove_pauses);
  $('#editor-export-name').value = state.exportNames[segment.id] || '';
  const sourceRemoved = Boolean(segment.source_removed);
  for (const selector of ['#editor-start', '#editor-end', '#editor-save-range']) $(selector).disabled = sourceRemoved;
  pauseToggle.disabled = sourceRemoved;
  pauseToggle.title = sourceRemoved ? t('Pause removal cannot be changed after the source recording is removed.') : '';
  const exportButton = $('#editor-export');
  exportButton.disabled = segment.rating !== 'accepted' || sourceRemoved;
  exportButton.textContent = sourceRemoved ? 'MP4 export unavailable after source removal.' : (segment.rating === 'accepted' ? 'Export MP4' : 'Approve before export');
  renderDetailedScoring(segment);
  renderTagFeedback(segment);
  setEditorTab(state.editorTab);
  document.querySelectorAll('.segment').forEach((article) => article.classList.toggle('editing', article.dataset.segmentId === segment.id));
}

async function saveSegmentRating(segment, rating) {
  const review_reason = rating === 'rejected' ? $('#editor-review-reason').value : '';
  await api(`/segments/${segment.id}`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({rating, review_reason}) });
  segment.rating = rating; segment.review_reason = review_reason;
  const original = state.activeResults?.find((item) => item.id === segment.id);
  if (original) { original.rating = rating; original.review_reason = review_reason; }
  await reloadActiveSegments();
  refreshStatisticsIfVisible();
}

async function playClipAudio(segment) {
  const player = $('#global-audio-player');
  state.listeningSegment = segment;
  $('#audio-now-playing-title').textContent = t(segment.remove_pauses ? 'Listening to dynamic clip' : 'Listening to clip');
  $('#audio-now-playing-detail').textContent = `${fmt(segment.start_seconds)} - ${fmt(segment.end_seconds)} / ${segment.transcript || t('No recognized speech')}`;
  $('#audio-now-playing').hidden = false;
  $('#listen-audio-track').value = String(state.listenAudioTrack || 1);
  await loadListeningAudio();
}

async function loadListeningAudio() {
  const segment = state.listeningSegment; if (!segment) return;
  const requestGeneration = ++state.listeningRequestGeneration;
  const player = $('#global-audio-player'); const track = Number($('#listen-audio-track').value || 1);
  state.listenAudioTrack = track;
  const removePauses = Boolean(segment.remove_pauses);
  const source = `/api/segments/${segment.id}/audio-preview?audio_track=${encodeURIComponent(track)}&remove_pauses=${removePauses}`;
  try {
    await api(`/segments/${segment.id}/audio-preview/check?audio_track=${encodeURIComponent(track)}&remove_pauses=${removePauses}`);
    if (requestGeneration !== state.listeningRequestGeneration || state.listeningSegment?.id !== segment.id) return;
    player.src = source; player.load(); await player.play();
  } catch (error) {
    if (requestGeneration !== state.listeningRequestGeneration || state.listeningSegment?.id !== segment.id) return;
    player.pause(); player.removeAttribute('src'); player.load(); message(error.message, true);
  }
}

function currentQuickClip() {
  return state.quickReview.clips[state.quickReview.index] || null;
}

function loadQuickReviewPreview(clip) {
  const player = $('#quick-review-video');
  const start = Number(clip.start_seconds || 0);
  const end = Number(clip.end_seconds || start + 1);
  const previewKey = `${clip.video_id}:${start.toFixed(3)}:${end.toFixed(3)}`;
  if (state.quickReview.previewKey === previewKey && player.getAttribute('src')) return;
  state.quickReview.previewKey = previewKey;
  player.pause();
  player.removeAttribute('src');
  player.load();
  player.src = `/api/videos/${clip.video_id}/stream#t=${start.toFixed(2)},${end.toFixed(2)}`;
  player.onloadedmetadata = () => {
    player.currentTime = start;
    player.play().catch(() => {});
  };
  player.load();
}

function clearQuickReviewPreview() {
  state.quickReview.previewKey = '';
  const player = $('#quick-review-video');
  player.pause();
  player.removeAttribute('src');
  player.load();
}

function renderQuickReview() {
  const clip = currentQuickClip();
  const total = state.quickReview.clips.length;
  if (!clip || !total) return;
  const reviewed = state.quickReview.clips.filter((item) => item.rating !== 'unrated').length;
  $('#quick-review-progress').textContent = `${state.quickReview.index + 1} / ${total}  |  reviewed: ${reviewed}`;
  $('#quick-review-time').textContent = `${fmt(clip.start_seconds)} - ${fmt(clip.end_seconds)}`;
  $('#quick-review-ranking').textContent = clip.ranking_score ? `Suggested score ${clip.ranking_score}/99` : `Quality ${clip.quality_score || 0}/99`;
  const tags = $('#quick-review-tags'); const visibleTags = contentTags(clip.tags); tags.replaceChildren(); for (const tag of visibleTags) tags.append(make('span', 'tag', tag)); tags.hidden = !visibleTags.length;
  $('#quick-review-transcript').textContent = clip.transcript || 'No recognized speech';
  $('#quick-review-context').textContent = '';
  $('#quick-review-approve').disabled = state.quickReview.saving;
  $('#quick-review-reject').disabled = state.quickReview.saving;
  $('#quick-review-previous').disabled = state.quickReview.saving || state.quickReview.index === 0;
  $('#quick-review-next').disabled = state.quickReview.saving || state.quickReview.index === total - 1;
  loadQuickReviewPreview(clip);
}

async function openQuickReview() {
  const requestedVideoId = state.videoId;
  const requestGeneration = ++state.quickReviewRequestGeneration;
  if (!requestedVideoId) return message('Choose a recording first.', true);
  try {
    const clips = await api(`/videos/${requestedVideoId}/top-clips?limit=20&unrated_only=true`);
    if (requestedVideoId !== state.videoId || requestGeneration !== state.quickReviewRequestGeneration) return;
    if (!clips.length) return message('There are no unreviewed candidates left for quick selection.');
    state.quickReview = { clips, index: 0, saving: false, previewKey: '' };
    globalAudioPlayer.pause(); globalAudioPlayer.removeAttribute('src'); globalAudioPlayer.load();
    $('#audio-now-playing').hidden = true;
    const dialog = $('#quick-review-dialog');
    if (!dialog.open) dialog.showModal();
    renderQuickReview();
  } catch (error) {
    if (requestedVideoId === state.videoId && requestGeneration === state.quickReviewRequestGeneration) message(error.message, true);
  }
}

async function rateQuickClip(rating) {
  const clip = currentQuickClip();
  if (!clip || state.quickReview.saving) return;
  state.quickReview.saving = true;
  renderQuickReview();
  try {
    await api(`/segments/${clip.id}`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({rating, review_reason:''}) });
    clip.rating = rating; clip.review_reason = '';
    const original = state.activeResults?.find((item) => item.id === clip.id);
    if (original) { original.rating = rating; original.review_reason = ''; }
    if (state.quickReview.index < state.quickReview.clips.length - 1) state.quickReview.index += 1;
    await reloadActiveSegments();
    refreshStatisticsIfVisible();
    renderQuickReview();
  } catch (error) { message(error.message, true); renderQuickReview(); }
  finally { state.quickReview.saving = false; renderQuickReview(); }
}

function moveQuickReview(offset) {
  const next = Math.max(0, Math.min(state.quickReview.clips.length - 1, state.quickReview.index + offset));
  if (next === state.quickReview.index || state.quickReview.saving) return;
  state.quickReview.index = next;
  renderQuickReview();
}

function closeQuickReview() {
  const dialog = $('#quick-review-dialog');
  if (dialog.open) dialog.close();
  clearQuickReviewPreview();
  globalAudioPlayer.pause(); globalAudioPlayer.removeAttribute('src'); globalAudioPlayer.load();
  $('#audio-now-playing').hidden = true;
}

async function loadSegments(custom = null) {
  const requestGeneration = ++state.segmentRequestGeneration;
  const requestedVideoId = state.videoId;
  if (!requestedVideoId) return;
  const selectedTag = $('#tag-search').value; const selectedRating = $('#rating-search').value; const hideReading = $('#hide-reading').checked && !['reading', 'format: czytanie'].includes(selectedTag);
  const source = custom
    ? custom.filter((segment) => (!selectedTag || (segment.tags || []).includes(selectedTag)) && (!selectedRating || segment.rating === selectedRating) && (!hideReading || Number(segment.reading_likelihood || 0) < 0.48))
    : await api(`/videos/${requestedVideoId}/segments?q=${encodeURIComponent($('#search').value)}&tag=${encodeURIComponent(selectedTag)}&rating=${encodeURIComponent(selectedRating)}&hide_reading=${hideReading}&show_duplicates=${$('#show-duplicates').checked}&sort=${encodeURIComponent($('#score-sort').value)}`);
  if (requestedVideoId !== state.videoId || requestGeneration !== state.segmentRequestGeneration) return;
  const values = sortSegments(source);
  const box = $('#segments'); const editingId = state.editingSegment?.id; let refreshedEditingSegment = null; box.replaceChildren();
  if (!values.length) {
    clearClipEditor();
    const emptyMessage = state.resultMode === 'all'
      ? t('No candidates match the active filters. Clear the text, tag or status filter to see all analysed clips.')
      : t('No candidates match the active prompt or reference collection.');
    box.append(make('p', 'hint', emptyMessage));
    return;
  }
  const template = $('#segment-template');
  for (const segment of values) {
    const node = template.content.cloneNode(true); const article = node.querySelector('article'); article.dataset.segmentId = segment.id; article.classList.add(segment.rating);
    const score = segment.similarity !== undefined ? ` / prompt ${Math.round(segment.similarity * 100)}%` : '';
    node.querySelector('.time').textContent = `${fmt(segment.start_seconds)} - ${fmt(segment.end_seconds)}${score}`;
    renderCardScoreGrid(node, segment);
    const tags = node.querySelector('.tags'); const visibleTags = contentTags(segment.tags);
    for (const tag of visibleTags) tags.append(makeTagPill(tag, segment.tag_feedback));
    tags.hidden = !visibleTags.length;
    node.querySelector('.transcript').textContent = segment.transcript || 'No recognized speech';
    renderClipContext(node, segment);
    renderChatReaction(node, segment);
    const openButton = node.querySelector('[data-open]');
    openButton.onclick = () => openSegmentInRecording(segment);
    if (segment.source_removed) { openButton.disabled = true; openButton.title = t('The source video was removed.'); }
    node.querySelectorAll('[data-rating]').forEach((button) => button.onclick = () => { selectClipForEditor(segment); saveSegmentRating(segment, button.dataset.rating).catch((error) => message(error.message, true)); });
    node.querySelector('[data-example]').onclick = async (event) => {
      if (!state.collectionId) return message('Choose a reference collection first.', true);
      const button = event.currentTarget; const collectionId = state.collectionId; button.disabled = true;
      try { await api(`/collections/${collectionId}/examples`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({segment_id:segment.id}) }); message('Reference added.'); await refreshLibrary(); }
      catch (error) { message(error.message, true); }
      finally { button.disabled = false; }
    };
    node.querySelector('[data-preview]').onclick = () => { playClipAudio(segment); };
    article.onclick = (event) => { if (!event.target.closest('button, a, audio, input, textarea, select, label, details')) selectClipForEditor(segment); };
    if (segment.id === editingId) refreshedEditingSegment = segment;
    box.append(node);
  }
  if (refreshedEditingSegment) selectClipForEditor(refreshedEditingSegment, state.clipEditorOpen);
  else if (editingId) clearClipEditor();
}

function sortSegments(segments) {
  const mode = $('#score-sort').value;
  const fields = {
    suggested_desc: ['ranking_score', -1], suggested_asc: ['ranking_score', 1],
    quality_desc: ['quality_score', -1], quality_asc: ['quality_score', 1],
    short_potential_desc: ['short_potential_score', -1], short_potential_asc: ['short_potential_score', 1],
    self_contained_desc: ['self_contained_score', -1], self_contained_asc: ['self_contained_score', 1],
  };
  const [field, direction] = fields[mode] || fields.suggested_desc;
  return [...segments].sort((left, right) => (Number(left[field] || 0) - Number(right[field] || 0)) * direction);
}

async function reloadActiveSegments() {
  await loadSegments(state.resultMode === 'all' ? null : (state.activeResults || []));
}

function renderClipContext(node, segment) {
  const summary = node.querySelector('.context-reaction'); const details = node.querySelector('.clip-context');
  summary.hidden = true;
  const before = String(segment.context_before || '').trim(); const after = String(segment.context_after || '').trim();
  if (!before && !after) { details.hidden = true; return; }
  node.querySelector('.context-before').textContent = before ? `${t('Before')}: ${before}` : `${t('Before')}: ${t('no recognised speech in the preceding 12 seconds.')}`;
  node.querySelector('.context-after').textContent = after ? `${t('After')}: ${after}` : `${t('After')}: ${t('no recognised speech in the following 12 seconds.')}`;
  details.hidden = false;
}

function renderChatReaction(node, segment) {
  const reaction = node.querySelector('.chat-reaction'); const messages = node.querySelector('.chat-messages'); const question = node.querySelector('.chat-question');
  const score = Number(segment.chat_reaction_score || 0); const count = Number(segment.chat_message_count || 0);
  question.textContent = '';
  question.hidden = true;
  reaction.textContent = '';
  reaction.hidden = true;
  if (!score || !count) { reaction.hidden = true; messages.hidden = true; return; }
  const previews = (segment.chat_messages || []).slice(0, 3).map((item) => `${item.author ? `${item.author}: ` : ''}${item.message}`).filter(Boolean);
  messages.textContent = previews.join('  |  '); messages.hidden = !previews.length;
}

function renderChatSummary(summary) {
  state.chat = summary;
  const box = $('#chat-summary'); const delay = $('#chat-delay');
  if (!summary?.available) { box.textContent = t('No chat transcript imported for this recording yet.'); return; }
  if (document.activeElement !== delay) delay.value = Number(summary.delay_seconds || 0);
  const authors = summary.unique_authors ? ` / ${summary.unique_authors} ${t('named viewers')}` : '';
  box.textContent = `${summary.source_name}: ${summary.message_count} ${t('messages')}${authors}. ${t('Delay')}: ${Number(summary.delay_seconds || 0).toFixed(1)} s.`;
}

async function loadChatSummary() {
  const requestGeneration = ++state.chatRequestGeneration;
  const requestedVideoId = state.videoId;
  if (!requestedVideoId) return;
  try {
    const summary = await api(`/videos/${requestedVideoId}/chat`);
    if (requestedVideoId !== state.videoId || requestGeneration !== state.chatRequestGeneration) return;
    renderChatSummary(summary);
  } catch (error) {
    if (requestedVideoId !== state.videoId || requestGeneration !== state.chatRequestGeneration) return;
    $('#chat-summary').textContent = `Chat data unavailable: ${error.message}`;
  }
}

async function checkForUpdates() {
  const status = $('#update-status'); const button = $('#check-updates'); const download = $('#download-update'); const install = $('#install-update');
  const notes = $('#update-release-notes'); const notesTitle = $('#update-release-title'); const notesBody = $('#update-release-body'); const notesLink = $('#update-release-link');
  button.disabled = true; download.hidden = true; install.hidden = true; notes.hidden = true; $('#update-download-progress').hidden = true; status.textContent = t('Checking GitHub releases...');
  try {
    const update = await api('/update-status');
    if (update.error) { status.textContent = `Version ${update.current_version}: ${update.error}`; return; }
    rememberStartupUpdate(update);
    if (update.release_notes || update.release_url) {
      notesTitle.textContent = update.release_name || `ClipFinder ${update.latest_version}`;
      notesBody.textContent = update.release_notes || t('No release notes were provided.');
      notesLink.href = update.release_url || '#'; notesLink.hidden = !update.release_url;
      notes.hidden = false;
    }
    if (!update.update_available) { status.textContent = `ClipFinder ${update.current_version} ${t('is up to date')} (${t('latest')}: ${update.latest_version}).`; return; }
    const size = update.asset_size ? ` (${bytes(update.asset_size)})` : '';
    const compact = update.update_kind === 'patch';
    const manualUrl = update.manual_download_url || update.download_url || '';
    download.href = manualUrl || '#'; download.hidden = !manualUrl;
    download.textContent = t('Download full installer');
    if (update.automatic_install_available) {
      status.textContent = `${t(compact ? 'Compact update' : 'Full update')} ${t('Update available').toLowerCase()}: ${update.current_version} -> ${update.latest_version}${size}.`;
      install.textContent = compact ? t('Download compact update') : t('Download update'); install.onclick = startAutomaticUpdate; install.hidden = false;
    } else {
      status.textContent = `${t('Update available')}: ${update.current_version} -> ${update.latest_version}. ${t('Download the full installer, close ClipFinder, then run it to update in place.')}`;
    }
  } catch (error) { status.textContent = `${t('Could not check for updates')}: ${error.message}`; }
  finally { button.disabled = false; }
}

async function startAutomaticUpdate() {
  const status = $('#update-status'); const install = $('#install-update'); const progress = $('#update-download-progress'); const fill = $('#update-download-fill');
  if (state.updatePollTimer) window.clearTimeout(state.updatePollTimer);
  state.updatePollTimer = null;
  const pollGeneration = ++state.updatePollGeneration;
  try {
    install.disabled = true; status.textContent = t('Preparing update download...'); progress.hidden = false; fill.style.width = '0%';
    const job = await api('/updates/download', { method:'POST' });
    if (pollGeneration !== state.updatePollGeneration) return;
    const jobId = job.id; state.updateDownloadId = jobId;
    const poll = async () => {
      try {
        const current = await api(`/updates/downloads/${jobId}`);
        if (pollGeneration !== state.updatePollGeneration || state.updateDownloadId !== jobId) return;
        const percent = Number(current.progress || 0); fill.style.width = `${percent}%`;
        const amount = current.total_bytes ? ` ${bytes(current.downloaded_bytes)} / ${bytes(current.total_bytes)}` : '';
        status.textContent = `${t(current.message || 'Downloading update')}${amount}${percent ? ` (${percent}%)` : ''}`;
        if (current.state === 'completed') {
          fill.style.width = '100%'; const compact = current.update_kind === 'patch'; status.textContent = t(compact ? 'Compact update is ready. ClipFinder will close, verify changed files and reopen.' : 'Update is ready. ClipFinder will close, install the update, then reopen.');
          state.updatePollTimer = null; install.disabled = false; install.textContent = t(compact ? 'Restart and apply update' : 'Restart and install update'); install.onclick = installAutomaticUpdate; return;
        }
        if (current.state === 'failed') { state.updatePollTimer = null; install.disabled = false; status.textContent = `${t('Update download failed')}: ${current.message}`; return; }
        state.updatePollTimer = window.setTimeout(poll, 700);
      } catch (error) {
        if (pollGeneration !== state.updatePollGeneration) return;
        state.updatePollTimer = null; install.disabled = false; status.textContent = `${t('Update download failed')}: ${error.message}`;
      }
    };
    void poll();
  } catch (error) {
    if (pollGeneration !== state.updatePollGeneration) return;
    state.updatePollTimer = null; install.disabled = false; status.textContent = `${t('Could not start the update')}: ${error.message}`;
  }
}

async function installAutomaticUpdate() {
  const status = $('#update-status'); const install = $('#install-update');
  try {
    install.disabled = true; status.textContent = t('Closing ClipFinder and installing the update...');
    await api(`/updates/downloads/${state.updateDownloadId}/install`, { method:'POST' });
  } catch (error) { install.disabled = false; status.textContent = `${t('Could not install the update')}: ${error.message}`; }
}

async function openDiagnosticReport() {
  const button = $('#download-diagnostics');
  button.disabled = true;
  try {
    const response = await api('/diagnostics/report');
    $('#diagnostics-report-content').value = await response.text();
    const dialog = $('#diagnostics-dialog');
    if (!dialog.open) dialog.showModal();
  } catch (error) {
    message(`Could not open diagnostic report: ${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

async function loadCollections() {
  const collections = await api('/collections'); const box = $('#collections'); box.replaceChildren();
  setSavedListCount('#collections-count', collections.length);
  if (state.collectionId && !collections.some((item) => item.id === state.collectionId)) { state.collectionId = null; state.collectionName = ''; }
  const target = $('#reference-url-collection'); const previousTarget = target.value; target.replaceChildren();
  for (const collection of collections) { const option = document.createElement('option'); option.value = collection.id; option.textContent = `${collection.name} (${collection.examples})`; target.append(option); }
  target.value = collections.some((item) => item.id === previousTarget) ? previousTarget : (state.collectionId || collections[0]?.id || '');
  for (const collection of collections) {
    const button = make('button', `collection ${state.collectionId === collection.id ? 'selected' : ''}`, `${collection.name} (${collection.examples})`);
    button.onclick = async () => {
      button.disabled = true;
      try { state.collectionId = collection.id; state.collectionName = collection.name; updateSelectionSummary(); await loadCollections(); await loadImportStatus(); }
      catch (error) { message(error.message, true); button.disabled = false; }
    };
    const remove = make('button', 'quiet collection-delete', 'Delete');
    remove.title = `Delete collection: ${collection.name}`;
    remove.onclick = async () => {
      if (!window.confirm(`Delete collection “${collection.name}” and all of its imported reference data? This cannot be undone.`)) return;
      try {
        await api(`/collections/${collection.id}`, { method:'DELETE' });
        if (state.collectionId === collection.id) { state.collectionId = null; state.collectionName = ''; }
        message(`Collection “${collection.name}” deleted.`); await refreshLibrary();
      } catch (error) { message(error.message, true); }
    };
    const row = make('div', 'collection-row'); row.append(button, remove); box.append(row);
  }
  updateSelectionSummary();
}

async function loadPrompts() {
  const prompts = await api('/prompts'); const box = $('#prompts'); box.replaceChildren();
  setSavedListCount('#prompts-count', prompts.length);
  for (const prompt of prompts) {
    const row = make('div', 'prompt-row'); const use = make('button', 'quiet', prompt.name); use.onclick = () => { $('#active-prompt').value = prompt.prompt; updateSelectionSummary(); };
    const remove = make('button', 'quiet collection-delete', 'Delete');
    remove.onclick = async () => { remove.disabled = true; try { await api(`/prompts/${prompt.id}`, { method:'DELETE' }); await loadPrompts(); } catch (error) { message(error.message, true); remove.disabled = false; } };
    row.append(use, remove); box.append(row);
  }
}

async function loadCaptionSettings() {
  const [defaults, favorites] = await Promise.all([api('/caption-defaults'), api('/caption-favorites')]);
  if (!state.captionDirty) {
    const saved = state.globalSession.caption || {};
    const settings = { ...defaults, ...saved, font_family: saved.font_family || defaults.font_family || 'Inter', outline_enabled: Boolean(saved.outline_enabled ?? Number(defaults.outline_enabled)), glow_enabled: Boolean(saved.glow_enabled ?? Number(defaults.glow_enabled)), opacity: Number(saved.opacity ?? defaults.opacity ?? 100) };
    state.globalCaption = settings;
    $('#global-caption-preset').value = settings.captions_preset;
    $('#global-caption-font-family').value = settings.font_family;
    $('#global-caption-base-color').value = settings.base_color.toLowerCase();
    $('#global-caption-active-color').value = settings.active_color.toLowerCase();
    $('#global-caption-outline-enabled').checked = settings.outline_enabled;
    $('#global-caption-outline-color').value = (settings.outline_color || '#000000').toLowerCase();
    $('#global-caption-glow-enabled').checked = settings.glow_enabled;
    $('#global-caption-opacity').value = String(settings.opacity);
    if (Object.keys(saved).length) state.captionDirty = true;
  }
  renderCaptionPreview();
  const box = $('#caption-favorites'); box.replaceChildren();
  setSavedListCount('#caption-favorites-count', favorites.length);
  for (const favorite of favorites) {
    const row = make('div', 'prompt-row');
    const use = make('button', 'quiet', `${favorite.name} (${favorite.captions_preset})`);
    use.onclick = () => {
      state.globalCaption = { captions_preset:favorite.captions_preset, base_color:favorite.base_color, active_color:favorite.active_color, font_family:favorite.font_family || 'Inter', outline_enabled:Boolean(Number(favorite.outline_enabled)), outline_color:favorite.outline_color || '#000000', glow_enabled:Boolean(Number(favorite.glow_enabled)), opacity:Number(favorite.opacity || 100) };
      state.captionDirty = true;
      $('#global-caption-preset').value = state.globalCaption.captions_preset; $('#global-caption-font-family').value = state.globalCaption.font_family;
      $('#global-caption-base-color').value = state.globalCaption.base_color.toLowerCase(); $('#global-caption-active-color').value = state.globalCaption.active_color.toLowerCase();
      $('#global-caption-outline-enabled').checked = state.globalCaption.outline_enabled; $('#global-caption-outline-color').value = state.globalCaption.outline_color.toLowerCase();
      $('#global-caption-glow-enabled').checked = state.globalCaption.glow_enabled; $('#global-caption-opacity').value = String(state.globalCaption.opacity);
      rememberGlobalSession(); renderCaptionPreview(); message(`Caption favorite “${favorite.name}” applied for this session.`);
    };
    const remove = make('button', 'quiet collection-delete', 'Delete');
    remove.onclick = async () => { try { await api(`/caption-favorites/${favorite.id}`, { method:'DELETE' }); await loadCaptionSettings(); } catch (error) { message(error.message, true); } };
    row.append(use, remove); box.append(row);
  }
}

async function loadExportSettings() {
  const defaults = await api('/export-defaults');
  if (!state.exportDirty) {
    const saved = state.globalSession.export || {};
    state.globalExport = { ...defaults, ...saved, layout_preset_id: saved.layout_preset_id || '', audio_track: Number(saved.audio_track ?? defaults.audio_track) };
    syncLayoutSelect();
    $('#global-audio-track').value = String(state.globalExport.audio_track);
    if (Object.keys(saved).length) state.exportDirty = true;
    drawLayoutOverlay();
  }
}

const layoutPresetValue = (id) => `preset:${id}`;
function friendlyLayoutName(layout) { return String(layout || 'original').replace('portrait_', 'vertical '); }
function syncLayoutSelect() {
  const select = $('#global-layout');
  if (!select) return;
  const presetId = String(state.globalExport.layout_preset_id || '');
  const presetValue = presetId ? layoutPresetValue(presetId) : '';
  select.value = presetValue && select.querySelector(`option[value="${presetValue}"]`)
    ? presetValue
    : state.globalExport.layout;
}
function applyLayoutPreset(preset, announce = true) {
  const fields = ['layout', 'camera_x', 'camera_y', 'camera_width', 'camera_height', 'game_x', 'game_y', 'game_width', 'game_height'];
  const values = Object.fromEntries(fields.map((field) => [field, preset[field]]));
  state.globalExport = { ...state.globalExport, ...values, layout_preset_id: preset.id, audio_track: Number(state.globalExport.audio_track) };
  state.exportDirty = true;
  syncLayoutSelect();
  $('#global-audio-track').value = String(state.globalExport.audio_track);
  rememberGlobalSession();
  drawLayoutOverlay();
  if (announce) message(`Layout preset "${preset.name}" is active for this session.`);
}
async function loadLayoutPresets() {
  const presets = await api('/layout-presets'); state.layoutPresets = presets;
  const select = $('#global-layout');
  select.querySelectorAll('option[data-layout-preset]').forEach((option) => option.remove());
  for (const preset of presets) {
    const option = document.createElement('option');
    option.value = layoutPresetValue(preset.id); option.dataset.layoutPreset = 'true';
    option.textContent = `${preset.name} - ${friendlyLayoutName(preset.layout)}`;
    select.append(option);
  }
  if (state.globalExport.layout_preset_id && !presets.some((preset) => String(preset.id) === String(state.globalExport.layout_preset_id))) {
    state.globalExport.layout_preset_id = '';
    rememberGlobalSession();
  }
  syncLayoutSelect();
  const box = $('#layout-presets'); box.replaceChildren();
  setSavedListCount('#layout-presets-count', presets.length);
  for (const preset of presets) {
    const row = make('div', 'prompt-row layout-preset-row');
    row.append(make('strong', 'saved-entry-name', `${preset.name} - ${friendlyLayoutName(preset.layout)}`));
    const actions = make('div', 'layout-preset-actions');
    const use = make('button', 'quiet', 'Use'); use.onclick = () => applyLayoutPreset(preset);
    const remove = make('button', 'quiet collection-delete', 'Delete'); remove.onclick = async () => { try { await api(`/layout-presets/${preset.id}`, {method:'DELETE'}); if (String(state.globalExport.layout_preset_id) === String(preset.id)) { state.globalExport.layout_preset_id = ''; state.exportDirty = true; rememberGlobalSession(); } await loadLayoutPresets(); } catch (error) { message(error.message, true); } };
    actions.append(use, remove); row.append(actions); box.append(row);
  }
}

function calibratedRect(kind) {
  const output = state.globalExport;
  return kind === 'camera'
    ? { x:Number(output.camera_x), y:Number(output.camera_y), width:Number(output.camera_width), height:Number(output.camera_height) }
    : { x:Number(output.game_x), y:Number(output.game_y), width:Number(output.game_width), height:Number(output.game_height) };
}
function storeCalibratedRect(kind, rect) {
  if (kind === 'camera') Object.assign(state.globalExport, { camera_x:rect.x, camera_y:rect.y, camera_width:rect.width, camera_height:rect.height });
  else Object.assign(state.globalExport, { game_x:rect.x, game_y:rect.y, game_width:rect.width, game_height:rect.height });
  state.globalExport.layout_preset_id = '';
  state.exportDirty = true;
  rememberGlobalSession();
}
function layoutCanvas() { return $('#layout-overlay'); }
function resizeLayoutCanvas() {
  const video = $('#layout-source-video'); const canvas = layoutCanvas();
  if (!video.videoWidth) return;
  const box = video.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(box.width)); canvas.height = Math.max(1, Math.round(box.height));
  canvas.style.width = `${box.width}px`; canvas.style.height = `${box.height}px`;
}
function rectanglePixels(rect, canvas) { return { x:rect.x * canvas.width, y:rect.y * canvas.height, width:rect.width * canvas.width, height:rect.height * canvas.height }; }
function drawRect(ctx, rect, color, label, canvas) {
  const box = rectanglePixels(rect, canvas); ctx.strokeStyle = color; ctx.fillStyle = `${color}22`; ctx.lineWidth = 3; ctx.fillRect(box.x, box.y, box.width, box.height); ctx.strokeRect(box.x, box.y, box.width, box.height); ctx.fillStyle = color; ctx.font = 'bold 13px system-ui'; ctx.fillText(label, box.x + 6, Math.max(16, box.y + 17));
}
function drawCroppedCover(ctx, video, rect, target) {
  const sourceX = rect.x * video.videoWidth; const sourceY = rect.y * video.videoHeight; const sourceW = rect.width * video.videoWidth; const sourceH = rect.height * video.videoHeight;
  const scale = Math.max(target.width / sourceW, target.height / sourceH); const width = sourceW * scale; const height = sourceH * scale;
  ctx.drawImage(video, sourceX, sourceY, sourceW, sourceH, target.x + (target.width - width) / 2, target.y + (target.height - height) / 2, width, height);
}
function drawCroppedContain(ctx, video, rect, target) {
  const sourceX = rect.x * video.videoWidth; const sourceY = rect.y * video.videoHeight; const sourceW = rect.width * video.videoWidth; const sourceH = rect.height * video.videoHeight;
  const scale = Math.min(target.width / sourceW, target.height / sourceH); const width = sourceW * scale; const height = sourceH * scale;
  ctx.drawImage(video, sourceX, sourceY, sourceW, sourceH, target.x + (target.width - width) / 2, target.y + (target.height - height) / 2, width, height);
}
function renderLayoutPreview() {
  const video = $('#layout-source-video'); const preview = $('#layout-output-preview'); if (!video.videoWidth) return;
  const ctx = preview.getContext('2d'); const camera = calibratedRect('camera'); const game = calibratedRect('game'); const layout = state.globalExport.layout;
  ctx.fillStyle = '#10141d'; ctx.fillRect(0, 0, preview.width, preview.height);
  try {
    if (layout === 'portrait_camera') drawCroppedCover(ctx, video, camera, {x:0,y:0,width:preview.width,height:preview.height});
    else if (layout === 'portrait_game') drawCroppedCover(ctx, video, game, {x:0,y:0,width:preview.width,height:preview.height});
    else if (layout === 'portrait_split') { drawCroppedContain(ctx, video, camera, {x:0,y:0,width:preview.width,height:preview.height / 3}); drawCroppedCover(ctx, video, game, {x:0,y:preview.height / 3,width:preview.width,height:preview.height * 2 / 3}); }
    else drawCroppedContain(ctx, video, {x:0,y:0,width:1,height:1}, {x:0,y:0,width:preview.width,height:preview.height});
  } catch { /* The video frame is simply not ready yet. */ }
}
function drawLayoutOverlay() {
  const video = $('#layout-source-video'); const canvas = layoutCanvas(); if (!video.videoWidth || !canvas.width) return;
  const ctx = canvas.getContext('2d'); ctx.clearRect(0, 0, canvas.width, canvas.height);
  drawRect(ctx, calibratedRect('camera'), '#77e3c0', 'Camera', canvas); drawRect(ctx, calibratedRect('game'), '#6db4ff', 'Gameplay', canvas);
  if (state.layoutCalibration.drawing) drawRect(ctx, state.layoutCalibration.drawing, '#ffd166', `New ${state.layoutCalibration.mode}`, canvas);
  renderLayoutPreview();
}
function canvasPoint(event) {
  const canvas = layoutCanvas(); const box = canvas.getBoundingClientRect();
  return { x:Math.max(0, Math.min(1, (event.clientX - box.left) / box.width)), y:Math.max(0, Math.min(1, (event.clientY - box.top) / box.height)) };
}
function setCalibrationMode(mode) {
  state.layoutCalibration.mode = mode; $('#layout-calibration-status').textContent = `Drawing ${mode} area: drag over the preview frame.`; layoutCanvas().classList.add('drawing');
}
function startLayoutPreview() {
  const video = state.videos.find((item) => item.id === state.videoId); if (!video) return message('Choose a recording first.', true);
  const player = $('#layout-source-video'); $('#layout-source-wrap').hidden = false; $('#layout-calibration-status').textContent = 'Pause on a representative frame, then draw the camera and gameplay areas.';
  player.src = `/api/videos/${video.id}/stream#t=1`; player.onloadedmetadata = () => { resizeLayoutCanvas(); drawLayoutOverlay(); }; player.onseeked = drawLayoutOverlay; player.ontimeupdate = drawLayoutOverlay; window.setTimeout(() => { resizeLayoutCanvas(); drawLayoutOverlay(); }, 300);
}

function stopLayoutPreview() {
  const player = $('#layout-source-video');
  if (!player) return;
  player.pause();
  player.onloadedmetadata = null;
  player.onseeked = null;
  player.ontimeupdate = null;
}

function updateAnalysisAudioModeUi() {
  const split = $('#analysis-audio-mode').value === 'split';
  $('#analysis-single-track-wrap').hidden = split;
  $('#analysis-split-options').hidden = !split;
  $('#analysis-all-sounds-track').disabled = !split || !$('#analysis-use-all-sounds').checked;
  $('#analysis-game-track').disabled = !split || !$('#analysis-use-game').checked;
}

async function loadAnalysisAudioSettings() {
  const defaults = await api('/analysis-audio-defaults');
  // The dashboard refreshes in the background. Never let it overwrite a
  // track/switch the user has changed but not saved yet.
  if (state.analysisAudioDirty) return;
  const saved = state.globalSession.analysisAudio || {};
  state.analysisAudio = { ...defaults, ...saved, use_all_sounds:Boolean(saved.use_all_sounds ?? defaults.use_all_sounds), use_game:Boolean(saved.use_game ?? defaults.use_game) };
  $('#analysis-audio-mode').value = state.analysisAudio.mode;
  $('#analysis-single-track').value = String(state.analysisAudio.single_track);
  $('#analysis-microphone-track').value = String(state.analysisAudio.microphone_track);
  $('#analysis-all-sounds-track').value = String(state.analysisAudio.all_sounds_track);
  $('#analysis-game-track').value = String(state.analysisAudio.game_track);
  $('#analysis-use-all-sounds').checked = state.analysisAudio.use_all_sounds;
  $('#analysis-use-game').checked = state.analysisAudio.use_game;
  if (Object.keys(saved).length) state.analysisAudioDirty = true;
  updateAnalysisAudioModeUi();
}

async function loadDiscoverySettings() {
  const defaults = await api('/discovery-defaults');
  if (state.discoveryDirty) return;
  state.discovery = defaults;
  const select = $('#discovery-profile'); const previous = select.value; select.replaceChildren();
  for (const profile of defaults.profiles) { const option = document.createElement('option'); option.value = profile.id; option.textContent = `${t(profile.name)} (${profile.accepted || 0} ${t('approved')} / ${profile.rejected || 0} ${t('rejected')})`; select.append(option); }
  const savedProfile = state.globalSession.discoveryProfile;
  select.value = defaults.profiles.some((profile) => profile.id === savedProfile) ? savedProfile : (defaults.active_profile || previous || 'general');
  if (savedProfile && select.value === savedProfile) state.discoveryDirty = true;
  const profanityFilter = $('#discovery-profanity-filter'); const savedProfanityFilter = state.globalSession.discoveryProfanityFilter;
  profanityFilter.value = ['allow', 'one', 'none'].includes(savedProfanityFilter) ? savedProfanityFilter : (defaults.profanity_filter || 'allow');
  if (savedProfanityFilter && profanityFilter.value === savedProfanityFilter) state.discoveryDirty = true;
  renderDiscoveryPatternSets();
  const active = defaults.profiles.find((profile) => profile.id === select.value) || {accepted:0, rejected:0};
  const activePattern = (defaults.pattern_sets || []).find((item) => item.id === $('#discovery-pattern-set').value);
  $('#discovery-feedback').textContent = discoveryFeedbackText(active, activePattern);
}

function discoveryFeedbackText(active, activePattern) {
  const summary = `${t('Learning data for this profile')}: ${active.accepted || 0} ${t('approved')}, ${active.rejected || 0} ${t('rejected')}.`;
  return activePattern
    ? `${summary} ${t('Pattern add-on')}: ${activePattern.name} (${activePattern.examples || 0} ${t('analysed Shorts')}).`
    : `${summary} ${t('Default mode')}: ${t('no additional Short patterns')}.`;
}

function renderDiscoveryPatternSets() {
  const profile = $('#discovery-profile').value || 'general';
  const sets = (state.discovery.pattern_sets || []).filter((item) => item.profile === profile);
  const select = $('#discovery-pattern-set'); const previous = select.value || state.globalSession.discoveryPatternSet || state.discovery.pattern_set_id || '';
  select.replaceChildren(); select.append(new Option(t('Default - no additional patterns'), ''));
  for (const patternSet of sets) select.append(new Option(`${patternSet.name} (${patternSet.examples || 0} ${t('analysed Shorts')})`, patternSet.id));
  select.value = sets.some((item) => item.id === previous) ? previous : '';
  const box = $('#discovery-pattern-sets'); box.replaceChildren(); setSavedListCount('#discovery-pattern-sets-count', sets.length);
  for (const patternSet of sets) {
    const row = make('div', 'prompt-row'); const name = make('span', 'saved-entry-name', `${patternSet.name} (${patternSet.examples || 0})`);
    const use = make('button', 'quiet', 'Use'); use.onclick = () => { select.value = patternSet.id; state.discoveryDirty = true; rememberGlobalSession(); updateDiscoveryPatternFeedback(); };
    const remove = make('button', 'quiet collection-delete', 'Delete'); remove.onclick = async () => { if (!window.confirm(`Delete pattern set "${patternSet.name}" and its analytical fingerprints?`)) return; try { await api(`/discovery-pattern-sets/${patternSet.id}`, {method:'DELETE'}); state.discoveryDirty = false; await loadDiscoverySettings(); message('Pattern set deleted.'); } catch (error) { message(error.message, true); } };
    row.append(name, use, remove); box.append(row);
  }
}

function updateDiscoveryPatternFeedback() {
  const active = state.discovery.profiles?.find((profile) => profile.id === $('#discovery-profile').value) || {accepted:0, rejected:0};
  const activePattern = (state.discovery.pattern_sets || []).find((item) => item.id === $('#discovery-pattern-set').value);
  $('#discovery-feedback').textContent = discoveryFeedbackText(active, activePattern);
}

function rememberAnalysisAudio() {
  state.analysisAudio = {
    mode: $('#analysis-audio-mode').value,
    single_track: Number($('#analysis-single-track').value),
    microphone_track: Number($('#analysis-microphone-track').value),
    all_sounds_track: Number($('#analysis-all-sounds-track').value),
    game_track: Number($('#analysis-game-track').value),
    use_all_sounds: $('#analysis-use-all-sounds').checked,
    use_game: $('#analysis-use-game').checked,
  };
  state.analysisAudioDirty = true;
  updateAnalysisAudioModeUi();
  rememberGlobalSession();
}

async function loadReferenceSources() {
  const sources = await api('/reference-sources'); const box = $('#reference-sources'); box.replaceChildren();
  for (const source of sources) {
    const row = make('div', 'source-row'); row.append(make('strong', '', `${source.collection_name} (${source.imported_examples} clips in collection)`), make('p', '', source.folder_path));
    const use = make('button', 'quiet', 'Use collection');
    use.onclick = async () => { use.disabled = true; try { state.collectionId = source.collection_id; state.collectionName = source.collection_name; updateSelectionSummary(); await loadCollections(); await loadImportStatus(); } catch (error) { message(error.message, true); use.disabled = false; } };
    const reimport = make('button', 'quiet', 'Reimport folder');
    reimport.onclick = async () => { reimport.disabled = true; try { await api(`/reference-sources/${source.id}/imports`, { method:'POST' }); message('Saved folder queued for import.'); state.collectionId = source.collection_id; state.collectionName = source.collection_name; await loadImportStatus(); } catch (error) { message(error.message, true); reimport.disabled = false; } };
    row.append(use, reimport); box.append(row);
  }
}

async function loadImportStatus() {
  const requestGeneration = ++state.importRequestGeneration;
  const requestedCollectionId = state.collectionId;
  const box = $('#import-status');
  if (!requestedCollectionId) {
    state.hasActiveImports = false;
    state.importRenderSignature = '';
    box.replaceChildren();
    return;
  }
  const imports = await api(`/collections/${requestedCollectionId}/imports`);
  if (requestedCollectionId !== state.collectionId || requestGeneration !== state.importRequestGeneration) return;
  state.hasActiveImports = imports.some((item) => ['queued', 'running'].includes(item.state));
  const renderSignature = JSON.stringify({ collectionId:requestedCollectionId, imports });
  if (renderSignature === state.importRenderSignature) return;
  state.importRenderSignature = renderSignature;
  box.replaceChildren();
  for (const item of imports) {
    const row = make('div', 'import-row', `${t(item.state)}: ${item.message}`);
    const track = make('div', 'progress-track'); const fill = make('div', 'progress-fill'); fill.style.width = `${clamp(item.progress)}%`; track.append(fill); row.append(track);
    if (['queued', 'running'].includes(item.state)) {
      const cancel = make('button', 'quiet danger-button', 'Cancel import');
      cancel.onclick = async () => { cancel.disabled = true; try { await api(`/reference-imports/${item.id}/cancel`, {method:'POST'}); await loadImportStatus(); } catch (error) { message(error.message, true); cancel.disabled = false; } };
      row.append(cancel);
    }
    box.append(row);
  }
}

async function loadRejectionReasons() {
  state.rejectionReasons = (await api('/rejection-reasons')).map((item) => item.reason);
  const box = $('#saved-rejection-reasons'); box.replaceChildren();
  setSavedListCount('#rejection-reasons-count', state.rejectionReasons.length);
  for (const reason of state.rejectionReasons) {
    const row = make('div', 'prompt-row'); row.append(make('span', 'saved-entry-name', reason));
    const remove = make('button', 'quiet collection-delete', 'Delete');
    remove.onclick = async () => {
      if (!window.confirm(`Remove saved rejection reason “${reason}”? Existing clips will keep their recorded reason.`)) return;
      try { await api(`/rejection-reasons/${encodeURIComponent(reason)}`, { method:'DELETE' }); await loadRejectionReasons(); message('Saved rejection reason removed.'); }
      catch (error) { message(error.message, true); }
    };
    row.append(remove); box.append(row);
  }
  document.querySelectorAll('[data-review-reason]').forEach(addRejectionReasons);
  if (state.editingSegment) addRejectionReasons($('#editor-review-reason'));
}
function statRow(label, value) { const row = make('div', 'statistics-row'); row.append(make('strong', '', label), make('span', '', value)); return row; }
function statAverage(value) { return value === null || value === undefined ? '—' : `${Math.round(value)}/99`; }
async function loadStatistics() {
  const overview = $('#statistics-overview'); if (!overview) return;
  try {
    const stats = await api('/statistics'); const data = stats.overview || {};
    overview.replaceChildren();
    const metrics = [['Reviewed', data.reviewed || 0], ['Approved', data.accepted || 0], ['Rejected', data.rejected || 0], ['Approval rate', data.approval_rate === null || data.approval_rate === undefined ? '—' : `${data.approval_rate}%`]];
    for (const [label, value] of metrics) { const card = make('div', 'statistics-metric'); card.append(make('strong', '', String(value)), make('span', '', label)); overview.append(card); }
    const reasons = $('#statistics-reasons'); reasons.replaceChildren();
    if (stats.rejection_reasons?.length) for (const item of stats.rejection_reasons) reasons.append(statRow(item.reason, `${item.count} ${t('rejected')}`));
    else reasons.append(make('p', 'statistics-empty', 'No rejected clips with saved reasons yet.'));
    const scores = $('#statistics-scores'); scores.replaceChildren();
    const labels = { quality:'Quality', short_potential:'Short potential', self_contained:'Self-contained', extended_completeness:'Extended completeness' };
    for (const [key, values] of Object.entries(stats.score_comparison || {})) scores.append(statRow(t(labels[key] || key), `${t('approved')} ${statAverage(values.accepted)} · ${t('rejected')} ${statAverage(values.rejected)}`));
    const tags = $('#statistics-tags'); tags.replaceChildren();
    if (stats.tags?.length) for (const item of stats.tags) { const rate = item.approval_rate === null || item.approval_rate === undefined ? t('no decisions') : `${item.approval_rate}% ${t('approved')}`; tags.append(statRow(item.tag, `${item.accepted} ${t('approved')} · ${item.rejected} ${t('rejected')} · ${rate}`)); }
    else tags.append(make('p', 'statistics-empty', 'No analysed tags yet.'));
    const diagnostics = $('#statistics-diagnostics'); diagnostics.replaceChildren();
    for (const item of stats.analysis_modes || []) diagnostics.append(statRow(`${t({ fast:'Fast', default:'Default', extended:'Extended' }[item.mode] || item.mode)} ${t('analysis')}`, `${item.accepted} ${t('approved')} · ${item.rejected} ${t('rejected')} · ${item.unrated} ${t('unreviewed')}`));
    const reading = stats.reading_flags || {}; diagnostics.append(statRow(t('Possible reading'), `${reading.accepted || 0} ${t('approved')} · ${reading.rejected || 0} ${t('rejected')} · ${reading.unrated || 0} ${t('unreviewed')}`));
  } catch (error) { overview.replaceChildren(make('p', 'statistics-empty', `Statistics could not be loaded: ${error.message}`)); }
}
function refreshStatisticsIfVisible() { if (document.querySelector('[data-setup-panel="statistics"]')?.classList.contains('active')) void loadStatistics(); }
async function refreshLibrary() { await Promise.all([loadCollections(), loadPrompts(), loadReferenceSources(), loadCaptionSettings(), loadExportSettings(), loadLayoutPresets(), loadAnalysisAudioSettings(), loadDiscoverySettings(), loadRejectionReasons()]); if (state.collectionId) await loadImportStatus(); }
async function refreshDashboard(full = false) {
  if (state.dashboardRefreshPromise) {
    if (!full) return state.dashboardRefreshPromise;
    try { await state.dashboardRefreshPromise; } catch { /* The full refresh below retries every request. */ }
  }
  const request = (async () => {
    try {
      await Promise.all([
        loadVideos(full),
        full ? refreshLibrary() : Promise.resolve(),
        !full && state.collectionId ? loadImportStatus() : Promise.resolve(),
      ]);
      const current = state.videos.find((video) => video.id === state.videoId);
      const shouldLoadCompletedAnalysis = current?.status === 'ready' && state.resultMode === 'all' && state.loadedReadyVideoId !== current.id;
      if (shouldLoadCompletedAnalysis) { await loadSegments(); state.loadedReadyVideoId = current.id; }
    } catch (error) { message(`Reconnecting to local API: ${error.message}`, true); }
  })();
  state.dashboardRefreshPromise = request;
  try { return await request; }
  finally { if (state.dashboardRefreshPromise === request) state.dashboardRefreshPromise = null; }
}

function selectedEditorSegment() {
  if (state.editingSegment) return state.editingSegment;
  message('Select a clip first.', true); return null;
}

function renderEditorCaptionPositionPreview(position) {
  const preview = $('#editor-caption-position-preview');
  if (!preview) return;
  preview.dataset.position = position || 'bottom';
  const labels = { top:'Top', two_fifths:'2/5 height', middle:'Middle', four_fifths:'4/5 height', bottom:'Bottom' };
  preview.setAttribute('aria-label', `Caption height preview: ${labels[position] || labels.bottom}`);
}
$('#editor-caption-position').onchange = () => { const segment = selectedEditorSegment(); if (segment) state.captionPositions[segment.id] = $('#editor-caption-position').value; renderEditorCaptionPositionPreview($('#editor-caption-position').value); };
$('#editor-export-name').oninput = () => { const segment = selectedEditorSegment(); if (segment) state.exportNames[segment.id] = $('#editor-export-name').value; };
$('#editor-save-range').onclick = async () => {
  const segment = selectedEditorSegment(); if (!segment) return;
  const start_seconds = Number($('#editor-start').value); const end_seconds = Number($('#editor-end').value);
  if (!Number.isFinite(start_seconds) || !Number.isFinite(end_seconds) || end_seconds <= start_seconds) return message('Enter a valid clip range.', true);
  const button = $('#editor-save-range'); button.disabled = true; button.textContent = 'Updating captions...';
  try { const updated = await api(`/segments/${segment.id}/timing`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({start_seconds, end_seconds}) }); Object.assign(segment, updated); const original = state.activeResults?.find((item) => item.id === segment.id); if (original) Object.assign(original, updated); message('Clip range and captions updated.'); await reloadActiveSegments(); }
  catch (error) { message(error.message, true); } finally { button.disabled = false; button.textContent = 'Save range'; }
};
$('#editor-save-transcript').onclick = async () => {
  const segment = selectedEditorSegment(); if (!segment) return;
  const button = $('#editor-save-transcript'); button.disabled = true;
  try { const updated = await api(`/segments/${segment.id}/transcript`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({transcript:$('#editor-transcript').value}) }); Object.assign(segment, updated); const original = state.activeResults?.find((item) => item.id === segment.id); if (original) Object.assign(original, updated); message('Caption text saved. The tags and search data were updated too.'); await reloadActiveSegments(); }
  catch (error) { message(error.message, true); } finally { button.disabled = false; }
};
$('#editor-save-rating').onclick = async () => { const segment = selectedEditorSegment(); if (!segment) return; try { await saveSegmentRating(segment, $('#editor-rating-select').value); } catch (error) { message(error.message, true); } };
$('#editor-censor-profanity').onchange = async () => {
  const segment = selectedEditorSegment(); if (!segment) return;
  const toggle = $('#editor-censor-profanity'); toggle.disabled = true;
  try { const updated = await api(`/segments/${segment.id}/censor`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({censor_profanity:toggle.checked}) }); Object.assign(segment, updated); const original = state.activeResults?.find((item) => item.id === segment.id); if (original) Object.assign(original, updated); message(updated.censor_profanity ? 'Profanity censoring enabled for this clip export.' : 'Profanity censoring disabled for this clip export.'); }
  catch (error) { toggle.checked = !toggle.checked; message(error.message, true); } finally { toggle.disabled = false; }
};
$('#editor-remove-pauses').onchange = async () => {
  const segment = selectedEditorSegment(); if (!segment) return;
  const toggle = $('#editor-remove-pauses'); toggle.disabled = true;
  try { const updated = await api(`/segments/${segment.id}/pause-trim`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({remove_pauses:toggle.checked}) }); Object.assign(segment, updated); const original = state.activeResults?.find((item) => item.id === segment.id); if (original) Object.assign(original, updated); if (state.listeningSegment?.id === segment.id) await loadListeningAudio(); message(updated.remove_pauses ? 'Long pauses will be removed in preview and export.' : 'Preview and export will keep original pauses.'); }
  catch (error) { toggle.checked = !toggle.checked; message(error.message, true); } finally { toggle.disabled = false; }
};
$('#editor-export').onclick = () => {
  const segment = selectedEditorSegment(); if (!segment || segment.rating !== 'accepted') return;
  const position = state.captionPositions[segment.id] || $('#editor-caption-position').value; const settings = state.globalCaption; const output = state.globalExport; const filename = state.exportNames[segment.id] || $('#editor-export-name').value;
  window.location.href = `/api/segments/${segment.id}/export?captions_preset=${encodeURIComponent(settings.captions_preset)}&caption_position=${encodeURIComponent(position)}&base_color=${encodeURIComponent(settings.base_color)}&active_color=${encodeURIComponent(settings.active_color)}&outline_enabled=${encodeURIComponent(settings.outline_enabled)}&outline_color=${encodeURIComponent(settings.outline_color)}&glow_enabled=${encodeURIComponent(settings.glow_enabled)}&opacity=${encodeURIComponent(settings.opacity)}&layout=${encodeURIComponent(output.layout)}&audio_track=${encodeURIComponent(output.audio_track)}&filename=${encodeURIComponent(filename)}${exportLayoutQuery(output)}`;
};
const globalAudioPlayer = $('#global-audio-player');
globalAudioPlayer.onplay = () => { if (state.previewAudio && state.previewAudio !== globalAudioPlayer) state.previewAudio.pause(); state.previewAudio = globalAudioPlayer; };
globalAudioPlayer.onpause = () => { if (state.previewAudio === globalAudioPlayer) state.previewAudio = null; };
globalAudioPlayer.onended = () => { if (state.previewAudio === globalAudioPlayer) state.previewAudio = null; $('#audio-now-playing').hidden = true; };
globalAudioPlayer.onerror = () => { $('#audio-now-playing').hidden = true; message('Audio preview could not be played.', true); };
$('#stop-listening').onclick = () => { state.listeningRequestGeneration += 1; state.listeningSegment = null; globalAudioPlayer.pause(); globalAudioPlayer.removeAttribute('src'); globalAudioPlayer.load(); $('#audio-now-playing').hidden = true; };
$('#listen-audio-track').onchange = () => { if (state.listeningSegment) loadListeningAudio().catch((error) => message(error.message, true)); };

async function uploadChatTranscript(videoId, file, delay) {
  const data = new FormData(); data.append('chat_file', file); data.append('delay_seconds', String(delay));
  const response = await fetch(`/api/videos/${videoId}/chat`, { method:'POST', body:data });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || 'Chat import failed.');
  return body;
}

$('#chat-import-form').onsubmit = async (event) => {
  event.preventDefault();
  const requestedVideoId = state.videoId;
  if (!requestedVideoId) return message('Choose a recording first.', true);
  const file = $('#chat-file').files[0]; const delay = Number($('#chat-delay').value);
  if (!file) return message('Choose a chat transcript first.', true);
  if (!Number.isFinite(delay) || delay < 0 || delay > 60) return message('Enter a chat delay between 0 and 60 seconds.', true);
  const button = event.target.querySelector('button'); button.disabled = true; button.textContent = 'Importing chat...';
  try {
    const summary = await uploadChatTranscript(requestedVideoId, file, delay);
    if (requestedVideoId !== state.videoId) { message('Chat was imported for the previously selected recording.'); return; }
    renderChatSummary(summary); event.target.reset(); $('#chat-delay').value = delay; await reloadActiveSegments(); message('Chat imported and candidate scores recalculated.');
  }
  catch (error) { message(error.message, true); }
  finally { button.disabled = false; button.textContent = 'Import chat and score clips'; }
};

$('#chat-delay-form').onsubmit = async (event) => {
  event.preventDefault();
  const requestedVideoId = state.videoId;
  if (!requestedVideoId) return message('Choose a recording first.', true);
  const delay = Number($('#chat-delay').value);
  if (!Number.isFinite(delay) || delay < 0 || delay > 60) return message('Enter a chat delay between 0 and 60 seconds.', true);
  const button = event.target.querySelector('button'); button.disabled = true;
  try {
    const summary = await api(`/videos/${requestedVideoId}/chat`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({delay_seconds:delay}) });
    if (requestedVideoId !== state.videoId) { message('Chat delay was saved for the previously selected recording.'); return; }
    renderChatSummary(summary); await reloadActiveSegments(); message('Chat delay saved and candidate scores recalculated.');
  }
  catch (error) { message(error.message, true); }
  finally { button.disabled = false; }
};

$('#upload-form').onsubmit = async (event) => { event.preventDefault(); const file = $('#video-file').files[0]; const analysisMode = $('#analysis-mode').value; if (!file) return message('Choose a video file first.', true); const submit = event.target.querySelector('button'); submit.disabled = true; setUploadProgress(0, `Preparing ${file.name} for upload...`); try { await uploadVideo(file, analysisMode); setUploadProgress(100, 'Upload complete. Analysis runs in the background.'); message(`${analysisMode} analysis queued.`); event.target.reset(); await refreshDashboard(); } catch (error) { setUploadProgress(0, error.message, true); message(error.message, true); } finally { submit.disabled = false; } };
$('#remote-video-form').onsubmit = async (event) => { event.preventDefault(); const url = $('#remote-video-url').value.trim(); const analysisMode = $('#analysis-mode').value; const submit = event.target.querySelector('button'); if (!url) return message('Paste a YouTube or Twitch VOD link first.', true); submit.disabled = true; try { await api('/videos/from-url', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({source_url:url, analysis_mode:analysisMode}) }); event.target.reset(); message(`Download queued with ${analysisMode} analysis.`); await refreshDashboard(); } catch (error) { message(error.message, true); } finally { submit.disabled = false; } };
$('#analysis-mode').onchange = analysisModeDescription;
analysisModeDescription();
$('#rejection-reason-form').onsubmit = async (event) => { event.preventDefault(); const input = $('#new-rejection-reason'); const reason = input.value.trim(); if (!reason) return message('Enter a custom rejection reason first.', true); const button = $('#save-rejection-reason'); button.disabled = true; try { const saved = await api('/rejection-reasons', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({reason}) }); input.value = ''; await loadRejectionReasons(); document.querySelectorAll('[data-review-reason]').forEach((select) => { if (![...select.options].some((option) => option.value === saved.reason)) { const option = document.createElement('option'); option.value = saved.reason; option.textContent = saved.reason; select.append(option); } }); message(`Custom rejection reason saved: ${saved.reason}`); } catch (error) { message(error.message, true); } finally { button.disabled = false; } };
$('#collection-form').onsubmit = async (event) => { event.preventDefault(); try { const collection = await api('/collections', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:$('#collection-name').value})}); state.collectionId = collection.id; state.collectionName = collection.name; event.target.reset(); await refreshLibrary(); } catch (error) { message(error.message, true); } };
$('#prompt-form').onsubmit = async (event) => { event.preventDefault(); try { const prompt = await api('/prompts', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:$('#prompt-name').value, prompt:$('#prompt-text').value}) }); $('#active-prompt').value = prompt.prompt; event.target.reset(); updateSelectionSummary(); await loadPrompts(); } catch (error) { message(error.message, true); } };
function captionPreviewBackground() {
  const choice = $('#caption-preview-background').value;
  const custom = choice === 'custom';
  $('#caption-preview-custom-wrap').hidden = !custom;
  return custom ? $('#caption-preview-custom-color').value : choice;
}
function captionSettingsFromControls() {
  return {
    captions_preset: $('#global-caption-preset').value,
    base_color: $('#global-caption-base-color').value,
    active_color: $('#global-caption-active-color').value,
    font_family: $('#global-caption-font-family').value,
    outline_enabled: $('#global-caption-outline-enabled').checked,
    outline_color: $('#global-caption-outline-color').value,
    glow_enabled: $('#global-caption-glow-enabled').checked,
    opacity: Number($('#global-caption-opacity').value)
  };
}
function previewOutlineShadow(pixels, color, enabled) {
  if (!enabled || pixels <= 0) return '';
  return `-${pixels}px -${pixels}px 0 ${color}, ${pixels}px -${pixels}px 0 ${color}, -${pixels}px ${pixels}px 0 ${color}, ${pixels}px ${pixels}px 0 ${color}`;
}
function currentCaptionPreviewWords() { return state.interfaceLanguage === 'pl' ? ['To', 'są', 'napisy', 'testowe'] : captionPreviewWords; }
function showCaptionPreviewWord(index, wordSynced) {
  const text = $('#caption-preview').querySelector('.caption-preview-text');
  text.replaceChildren();
  const previewWords = currentCaptionPreviewWords();
  previewWords.forEach((word, wordIndex) => {
    const node = document.createElement('span');
    node.className = `caption-preview-word${wordSynced && wordIndex === index ? ' is-active' : ''}`;
    node.textContent = word;
    text.append(node);
    if (wordIndex < previewWords.length - 1) text.append(document.createTextNode(' '));
  });
}
function captionPreviewAnimationAllowed() {
  return !document.hidden
    && $('#setup-sidebar')?.classList.contains('open')
    && document.querySelector('[data-setup-panel="global"]')?.classList.contains('active');
}
function stopCaptionPreviewAnimation() {
  if (captionPreviewTimer) { window.clearInterval(captionPreviewTimer); captionPreviewTimer = null; }
}
function startCaptionPreviewAnimation(preset) {
  stopCaptionPreviewAnimation();
  showCaptionPreviewWord(0, Boolean(preset.active));
  if (!preset.active || !captionPreviewAnimationAllowed()) return;
  let wordIndex = 0;
  captionPreviewTimer = window.setInterval(() => {
    wordIndex = (wordIndex + 1) % currentCaptionPreviewWords().length;
    showCaptionPreviewWord(wordIndex, true);
  }, 720);
}
function renderCaptionPreview() {
  const preview = $('#caption-preview'); if (!preview) return;
  const settings = captionSettingsFromControls();
  const preset = captionPreviewPresets[settings.captions_preset] || captionPreviewPresets.clean;
  preview.dataset.preset = settings.captions_preset;
  preview.dataset.variant = preset.variant || '';
  preview.style.setProperty('--caption-preview-background', captionPreviewBackground());
  preview.style.setProperty('--caption-preview-base', settings.base_color);
  preview.style.setProperty('--caption-preview-active', settings.active_color);
  preview.style.setProperty('--caption-preview-active-display', preset.active ? settings.active_color : settings.base_color);
  preview.style.setProperty('--caption-preview-font', captionPreviewFontFamilies[settings.font_family] || captionPreviewFontFamilies.Inter);
  preview.style.setProperty('--caption-preview-size', preset.size || '25px');
  preview.style.setProperty('--caption-preview-weight', preset.weight || '700');
  preview.style.setProperty('--caption-preview-active-scale', preset.activeScale || '1');
  preview.style.setProperty('--caption-preview-active-weight', preset.activeWeight || preset.weight || '700');
  const outline = previewOutlineShadow(preset.outline || 0, settings.outline_color, settings.outline_enabled);
  const glow = settings.glow_enabled ? `0 0 7px ${preset.active ? settings.active_color : settings.base_color}, 0 0 15px ${preset.active ? settings.active_color : settings.base_color}` : '';
  preview.style.setProperty('--caption-preview-shadow', [outline, glow].filter(Boolean).join(', ') || 'none');
  preview.style.setProperty('--caption-preview-opacity', String(Math.max(.2, Math.min(1, settings.opacity / 100))));
  preview.querySelector('.caption-preview-text').hidden = !preset.showText && settings.captions_preset === 'none';
  preview.querySelector('.caption-preview-off').hidden = settings.captions_preset !== 'none';
  $('#caption-preview-hint').textContent = t(preset.hint);
  $('#global-caption-opacity-value').textContent = `${settings.opacity}%`;
  startCaptionPreviewAnimation(preset);
}
function rememberGlobalCaption() { state.globalCaption = captionSettingsFromControls(); state.captionDirty = true; rememberGlobalSession(); renderCaptionPreview(); }
$('#global-caption-preset').onchange = rememberGlobalCaption;
$('#global-caption-font-family').onchange = rememberGlobalCaption;
$('#global-caption-base-color').oninput = rememberGlobalCaption;
$('#global-caption-active-color').oninput = rememberGlobalCaption;
$('#global-caption-outline-enabled').onchange = rememberGlobalCaption;
$('#global-caption-outline-color').oninput = rememberGlobalCaption;
$('#global-caption-glow-enabled').onchange = rememberGlobalCaption;
$('#global-caption-opacity').oninput = rememberGlobalCaption;
$('#caption-preview-background').onchange = () => { try { localStorage.setItem('clipfinder-caption-preview-background', $('#caption-preview-background').value); } catch { /* Optional preference only. */ } renderCaptionPreview(); };
$('#caption-preview-custom-color').oninput = () => { try { localStorage.setItem('clipfinder-caption-preview-custom-color', $('#caption-preview-custom-color').value); } catch { /* Optional preference only. */ } renderCaptionPreview(); };
try {
  const previewBackground = localStorage.getItem('clipfinder-caption-preview-background');
  const previewCustomColor = localStorage.getItem('clipfinder-caption-preview-custom-color');
  if (previewBackground && [...$('#caption-preview-background').options].some((option) => option.value === previewBackground)) $('#caption-preview-background').value = previewBackground;
  if (previewCustomColor && /^#[0-9a-f]{6}$/i.test(previewCustomColor)) $('#caption-preview-custom-color').value = previewCustomColor;
} catch { /* Optional preference only. */ }
renderCaptionPreview();
$('#caption-favorite-form').onsubmit = async (event) => { event.preventDefault(); try { await api('/caption-favorites', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:$('#caption-favorite-name').value, ...captionSettingsFromControls()}) }); event.target.reset(); await loadCaptionSettings(); message('Caption favorite saved.'); } catch (error) { message(error.message, true); } };
function rememberGlobalLayout() {
  const selectedLayout = $('#global-layout').value;
  const presetId = selectedLayout.startsWith('preset:') ? selectedLayout.slice('preset:'.length) : '';
  const preset = presetId ? state.layoutPresets.find((item) => String(item.id) === presetId) : null;
  if (preset) {
    applyLayoutPreset(preset, false);
    return;
  }
  state.globalExport = { ...state.globalExport, layout: selectedLayout, layout_preset_id: '', audio_track: Number($('#global-audio-track').value) };
  state.exportDirty = true;
  rememberGlobalSession();
  renderLayoutPreview();
}
function rememberGlobalAudioTrack() {
  state.globalExport.audio_track = Number($('#global-audio-track').value || 1);
  state.exportDirty = true;
  rememberGlobalSession();
}
$('#global-layout').onchange = rememberGlobalLayout;
$('#global-audio-track').onchange = rememberGlobalAudioTrack;
$('#layout-preset-form').onsubmit = async (event) => { event.preventDefault(); const name = $('#layout-preset-name').value.trim(); if (!name) return; try { await api('/layout-presets', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...state.globalExport, name})}); event.target.reset(); await loadLayoutPresets(); message(`Layout "${name}" saved.`); } catch (error) { message(error.message, true); } };
$('#load-layout-preview').onclick = startLayoutPreview;
$('#calibrate-camera').onclick = () => setCalibrationMode('camera');
$('#calibrate-game').onclick = () => setCalibrationMode('game');
layoutCanvas().onpointerdown = (event) => { if (!$('#layout-source-video').videoWidth) return; const point = canvasPoint(event); state.layoutCalibration.start = point; state.layoutCalibration.drawing = {x:point.x, y:point.y, width:0, height:0}; layoutCanvas().setPointerCapture(event.pointerId); drawLayoutOverlay(); };
layoutCanvas().onpointermove = (event) => { const start = state.layoutCalibration.start; if (!start) return; const point = canvasPoint(event); state.layoutCalibration.drawing = {x:Math.min(start.x, point.x), y:Math.min(start.y, point.y), width:Math.abs(point.x - start.x), height:Math.abs(point.y - start.y)}; drawLayoutOverlay(); };
layoutCanvas().onpointerup = (event) => { const rect = state.layoutCalibration.drawing; if (!state.layoutCalibration.start || !rect) return; layoutCanvas().releasePointerCapture?.(event.pointerId); state.layoutCalibration.start = null; state.layoutCalibration.drawing = null; if (rect.width < .02 || rect.height < .02) { $('#layout-calibration-status').textContent = 'Area is too small. Drag a larger rectangle.'; drawLayoutOverlay(); return; } storeCalibratedRect(state.layoutCalibration.mode, rect); $('#layout-calibration-status').textContent = `${state.layoutCalibration.mode === 'camera' ? 'Camera' : 'Gameplay'} area updated for this session. Enter a name below only if you want to save it as a preset.`; layoutCanvas().classList.remove('drawing'); drawLayoutOverlay(); };
window.addEventListener('resize', () => { resizeLayoutCanvas(); drawLayoutOverlay(); });
['#analysis-audio-mode', '#analysis-single-track', '#analysis-microphone-track', '#analysis-all-sounds-track', '#analysis-game-track', '#analysis-use-all-sounds', '#analysis-use-game'].forEach((selector) => { $(selector).onchange = rememberAnalysisAudio; });
$('#analysis-audio-form').onsubmit = async (event) => { event.preventDefault(); const body = { mode:$('#analysis-audio-mode').value, single_track:Number($('#analysis-single-track').value), microphone_track:Number($('#analysis-microphone-track').value), all_sounds_track:Number($('#analysis-all-sounds-track').value), game_track:Number($('#analysis-game-track').value), use_all_sounds:$('#analysis-use-all-sounds').checked, use_game:$('#analysis-use-game').checked }; try { state.analysisAudio = await api('/analysis-audio-defaults', { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) }); state.analysisAudioDirty = false; rememberGlobalSession(); await loadAnalysisAudioSettings(); message('Analysis audio settings saved. They apply to the next analysis or reanalysis.'); } catch (error) { message(error.message, true); } };
$('#discovery-profile').onchange = () => { state.discoveryDirty = true; renderDiscoveryPatternSets(); updateDiscoveryPatternFeedback(); rememberGlobalSession(); };
$('#discovery-pattern-set').onchange = () => { state.discoveryDirty = true; updateDiscoveryPatternFeedback(); rememberGlobalSession(); };
$('#discovery-profanity-filter').onchange = () => { state.discoveryDirty = true; rememberGlobalSession(); };
$('#discovery-defaults-form').onsubmit = async (event) => { event.preventDefault(); try { state.discovery = await api('/discovery-defaults', { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({active_profile:$('#discovery-profile').value, pattern_set_id:$('#discovery-pattern-set').value, profanity_filter:$('#discovery-profanity-filter').value}) }); state.discoveryDirty = false; rememberGlobalSession(); await loadDiscoverySettings(); if (state.videoId) await showAllSegments(); message('Discovery profile, pattern add-on and profanity filter saved. Candidate ranking was refreshed.'); } catch (error) { message(error.message, true); } };
$('#discovery-pattern-set-form').onsubmit = async (event) => { event.preventDefault(); const name = $('#discovery-pattern-set-name').value.trim(); if (!name) return; try { const created = await api('/discovery-pattern-sets', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name, profile:$('#discovery-profile').value})}); $('#discovery-pattern-set-name').value = ''; state.discoveryDirty = false; state.discovery = await api('/discovery-defaults'); renderDiscoveryPatternSets(); $('#discovery-pattern-set').value = created.id; state.discoveryDirty = true; rememberGlobalSession(); updateDiscoveryPatternFeedback(); message(`Pattern set "${created.name}" created. Select Save discovery profile to activate it.`); } catch (error) { message(error.message, true); } };
$('#generate-prompt-button').onclick = async () => {
  const collectionId = state.collectionId; if (!collectionId) return;
  const button = $('#generate-prompt-button'); button.disabled = true;
  try {
    const suggested = await api(`/collections/${collectionId}/prompt-suggestion`, { method:'POST' });
    if (state.collectionId !== collectionId) return;
    $('#prompt-name').value = suggested.name; $('#prompt-text').value = suggested.prompt; $('#active-prompt').value = suggested.prompt; updateSelectionSummary(); message('Prompt generated from reference clips. Review it and click Save prompt.');
  } catch (error) { if (state.collectionId === collectionId) message(error.message, true); }
  finally { button.disabled = !state.collectionId; }
};
$('#import-folder-button').onclick = async () => { const folder_path = $('#reference-folder').value.trim(); if (!state.collectionId || !folder_path) return message('Choose a collection and enter the folder path.', true); try { await api(`/collections/${state.collectionId}/imports`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({folder_path, include_subfolders:$('#subfolders').checked}) }); message('Folder saved and queued for import.'); await loadReferenceSources(); await loadImportStatus(); } catch (error) { message(error.message, true); } };
$('#import-url-button').onclick = async () => { const source_url = $('#reference-url').value.trim(); const collectionId = $('#reference-url-collection').value; if (!collectionId || !source_url) return message('Choose a destination collection and enter a YouTube or TikTok link.', true); const button = $('#import-url-button'); button.disabled = true; try { await api(`/collections/${collectionId}/imports/from-url`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({source_url}) }); $('#reference-url').value = ''; message('Reference link queued for local download and indexing.'); if (state.collectionId === collectionId) await loadImportStatus(); await loadCollections(); } catch (error) { message(error.message, true); } finally { button.disabled = false; updateSelectionSummary(); } };
function renderRemotePreview(result) {
  const box = $('#remote-preview-result'); box.replaceChildren(); box.hidden = false;
  box.append(make('h4', '', result.title || 'Short/video preview'));
  const meta = make('p', 'remote-preview-meta', `Duration ${fmt(Number(result.duration_seconds || 0))} - quality ${result.quality_score || 0}/99 - logical sense ${result.logical_sense_score || 0}/99${Number(result.reading_likelihood || 0) >= .48 ? ' - possible reading' : ''}`);
  box.append(meta);
  if (result.frame_data_url) { const frame = document.createElement('img'); frame.src = result.frame_data_url; frame.alt = 'Temporary preview frame from the linked video'; box.append(frame); }
  if (result.tags?.length) { const tags = make('div', 'tags'); result.tags.forEach((tag) => tags.append(makeTagPill(tag))); box.append(tags); }
  if (result.quality_signals?.length) box.append(make('p', 'remote-preview-meta', result.quality_signals.join(' - ')));
  const transcript = make('p', 'preview-transcript', result.transcript || 'No speech was detected in this video.'); box.append(transcript);
  const link = document.createElement('a'); link.className = 'quiet'; link.href = result.source_url; link.target = '_blank'; link.rel = 'noopener'; link.textContent = 'Open original video'; box.append(link);
  const patternSet = (state.discovery.pattern_sets || []).find((item) => item.id === state.discovery.pattern_set_id);
  const savePattern = make('button', 'quiet', patternSet ? `Save analysis to patterns: ${patternSet.name}` : 'Choose a pattern add-on in Global first');
  savePattern.disabled = !patternSet || !state.remotePreview.completedJobId;
  savePattern.onclick = async () => {
    const activePatternSet = (state.discovery.pattern_sets || []).find((item) => item.id === state.discovery.pattern_set_id);
    const completedJobId = state.remotePreview.completedJobId;
    if (!activePatternSet || !completedJobId) return renderRemotePreview(result);
    savePattern.disabled = true;
    try {
      const saved = await api(`/remote-preview/${completedJobId}/save-pattern`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({pattern_set_id:activePatternSet.id})});
      message(`Analytical fingerprint saved to pattern set: ${saved.pattern_set_name}.`);
      state.remotePreview.completedJobId = null; state.discoveryDirty = false; await loadDiscoverySettings(); renderRemotePreview(result);
    } catch (error) { savePattern.disabled = false; message(`Could not save analytical fingerprint: ${error.message}`, true); }
  };
  box.append(savePattern);
  box.append(make('p', 'remote-preview-meta', result.retention || 'Temporary analysis only.'));
}
function setRemotePreviewStatus(job) {
  const box = $('#remote-preview-status'); box.replaceChildren();
  const label = make('div', 'import-row', `${job.message || 'Working'} (${clamp(job.progress)}%)`); box.append(label);
  const track = make('div', 'progress-track'); const fill = make('div', 'progress-fill'); fill.style.width = `${clamp(job.progress)}%`; if (job.state === 'failed') fill.style.background = 'var(--danger)'; track.append(fill); box.append(track);
}
async function pollRemotePreview() {
  if (!state.remotePreview.jobId) return;
  try {
    const job = await api(`/remote-preview/${state.remotePreview.jobId}`);
    setRemotePreviewStatus(job);
    if (job.state === 'completed') {
      state.remotePreview.completedJobId = state.remotePreview.jobId; state.remotePreview.jobId = null; state.remotePreview.poll = null; $('#remote-preview-button').disabled = false; renderRemotePreview(job.result || {}); message('Temporary Short/video preview completed.'); return;
    }
    if (job.state === 'failed') {
      state.remotePreview.jobId = null; state.remotePreview.poll = null; $('#remote-preview-button').disabled = false; message(`Preview analysis failed: ${job.message || 'Unknown error'}`, true); return;
    }
    state.remotePreview.poll = window.setTimeout(pollRemotePreview, 1200);
  } catch (error) {
    state.remotePreview.jobId = null; state.remotePreview.poll = null; $('#remote-preview-button').disabled = false; message(`Preview status failed: ${error.message}`, true);
  }
}
$('#remote-preview-button').onclick = async () => {
  const sourceUrl = $('#remote-preview-url').value.trim();
  if (!sourceUrl) return message('Enter one public YouTube Short/video or TikTok link.', true);
  const button = $('#remote-preview-button'); button.disabled = true;
  $('#remote-preview-result').hidden = true; $('#remote-preview-result').replaceChildren();
  state.remotePreview.completedJobId = null;
  if (state.remotePreview.poll) window.clearTimeout(state.remotePreview.poll);
  try {
    const started = await api('/remote-preview', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({source_url:sourceUrl}) });
    state.remotePreview.jobId = started.job_id;
    setRemotePreviewStatus({progress:0, message:'Queued temporary preview', state:'queued'});
    pollRemotePreview();
  } catch (error) {
    button.disabled = false; message(`Could not start preview: ${error.message}`, true);
  }
};
async function showAllSegments() {
  state.resultRequestGeneration += 1;
  state.resultMode = 'all'; state.activeResults = null;
  const video = state.videos.find((item) => item.id === state.videoId);
  if (video) $('#selected-title').textContent = `Candidates: ${video.original_name}`;
  try { await loadSegments(); } catch (error) { message(error.message, true); }
}
$('#search-button').onclick = showAllSegments;
$('#search').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); void showAllSegments(); } });
$('#tag-search-button').onclick = showAllSegments;
$('#rating-search-button').onclick = showAllSegments;
$('#tag-search').onchange = showAllSegments;
$('#rating-search').onchange = showAllSegments;
$('#hide-reading').onchange = showAllSegments;
$('#show-duplicates').onchange = showAllSegments;
$('#score-sort').onchange = () => reloadActiveSegments().catch((error) => message(error.message, true));
$('#top-clips-button').onclick = async () => {
  const requestedVideoId = state.videoId; const requestGeneration = ++state.resultRequestGeneration;
  if (!requestedVideoId) return message('Choose a recording first.', true);
  const limit = Number($('#best-of-limit').value) || 10;
  try {
    const results = await api(`/videos/${requestedVideoId}/top-clips?limit=${encodeURIComponent(limit)}`);
    if (requestedVideoId !== state.videoId || requestGeneration !== state.resultRequestGeneration) return;
    state.resultMode = 'top'; state.activeResults = results;
    $('#selected-title').textContent = `Best of stream: ${state.videos.find((video) => video.id === requestedVideoId)?.original_name || ''}`;
    await loadSegments(results);
    message(`Showing ${results.length} strong, distinct moments from this stream.`);
  } catch (error) {
    if (requestedVideoId === state.videoId && requestGeneration === state.resultRequestGeneration) message(error.message, true);
  }
};
$('#quick-review-button').onclick = openQuickReview;
$('#quick-review-close').onclick = closeQuickReview;
$('#quick-review-approve').onclick = () => rateQuickClip('accepted');
$('#quick-review-reject').onclick = () => rateQuickClip('rejected');
$('#quick-review-previous').onclick = () => moveQuickReview(-1);
$('#quick-review-next').onclick = () => moveQuickReview(1);
$('#quick-review-dialog').addEventListener('close', () => { clearQuickReviewPreview(); globalAudioPlayer.pause(); globalAudioPlayer.removeAttribute('src'); globalAudioPlayer.load(); $('#audio-now-playing').hidden = true; });
$('#description-button').onclick = async () => {
  const requestedVideoId = state.videoId; const description = $('#active-prompt').value.trim(); const requestGeneration = ++state.resultRequestGeneration;
  if (!requestedVideoId || description.length < 3) return message('Choose a recording and enter a prompt first.', true);
  try {
    const results = await api('/search/description', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({video_id:requestedVideoId, description}) });
    if (requestedVideoId !== state.videoId || requestGeneration !== state.resultRequestGeneration) return;
    state.resultMode = 'description'; state.activeResults = results; await loadSegments(results);
  } catch (error) {
    if (requestedVideoId === state.videoId && requestGeneration === state.resultRequestGeneration) message(error.message, true);
  }
};
$('#similar-button').onclick = async () => {
  const requestedVideoId = state.videoId; const requestedCollectionId = state.collectionId; const requestGeneration = ++state.resultRequestGeneration;
  if (!requestedVideoId || !requestedCollectionId) return message('Choose a recording and a collection first.', true);
  try {
    const results = await api(`/collections/${requestedCollectionId}/search`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({video_id:requestedVideoId})});
    if (requestedVideoId !== state.videoId || requestedCollectionId !== state.collectionId || requestGeneration !== state.resultRequestGeneration) return;
    state.resultMode = 'similar'; state.activeResults = results; await loadSegments(results);
  } catch (error) {
    if (requestedVideoId === state.videoId && requestedCollectionId === state.collectionId && requestGeneration === state.resultRequestGeneration) message(error.message, true);
  }
};
$('#active-prompt').oninput = updateSelectionSummary; $('#refresh').onclick = () => refreshDashboard(true);
$('#check-updates').onclick = checkForUpdates;
$('#startup-update-notice').onclick = openStartupUpdateNotice;
$('#download-diagnostics').onclick = openDiagnosticReport;
$('#close-diagnostics').onclick = () => $('#diagnostics-dialog').close();
$('#copy-diagnostics').onclick = async () => {
  const report = $('#diagnostics-report-content');
  try {
    await navigator.clipboard.writeText(report.value);
    message('Diagnostic report copied to clipboard.');
  } catch {
    report.focus(); report.select();
    message('Select the report and copy it with Ctrl+C.');
  }
};
function setSetupTab(tab) {
  const selected = ['search', 'global', 'statistics', 'options'].includes(tab) ? tab : 'search';
  document.querySelectorAll('[data-setup-tab]').forEach((button) => {
    const active = button.dataset.setupTab === selected;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
  });
  document.querySelectorAll('[data-setup-panel]').forEach((panel) => {
    const active = panel.dataset.setupPanel === selected;
    panel.classList.toggle('active', active);
    panel.hidden = !active;
  });
  try { localStorage.setItem('clipfinder-setup-tab', selected); } catch { /* Optional preference only. */ }
  if (selected === 'statistics') void loadStatistics();
  if (selected === 'global' && $('#setup-sidebar')?.classList.contains('open')) renderCaptionPreview();
  else { stopCaptionPreviewAnimation(); stopLayoutPreview(); }
}
function organizeSetupCards() {
  const searchGrid = document.querySelector('[data-setup-panel="search"] .library-grid');
  const optionsPanel = document.querySelector('[data-setup-panel="options"]');
  const discoveryCard = $('#discovery-defaults-form')?.closest('.library-card');
  const rejectionCard = $('#rejection-reason-form')?.closest('.library-card');
  if (searchGrid && discoveryCard) searchGrid.append(discoveryCard);
  if (optionsPanel && rejectionCard) optionsPanel.append(rejectionCard);
}
function setSetupSidebar(open) {
  const sidebar = $('#setup-sidebar'); const trigger = $('#setup-toggle');
  sidebar.classList.toggle('open', open);
  document.body.classList.toggle('setup-sidebar-open', open);
  document.querySelector('main').classList.toggle('setup-sidebar-open', open);
  sidebar.setAttribute('aria-hidden', String(!open));
  sidebar.inert = !open;
  trigger.setAttribute('aria-expanded', String(open));
  if (open && document.querySelector('[data-setup-panel="global"]')?.classList.contains('active')) renderCaptionPreview();
  else { stopCaptionPreviewAnimation(); stopLayoutPreview(); }
}
$('#setup-toggle').onclick = () => setSetupSidebar(!$('#setup-sidebar').classList.contains('open'));
$('#setup-close').onclick = () => setSetupSidebar(false);
document.querySelectorAll('[data-setup-tab]').forEach((button) => { button.onclick = () => setSetupTab(button.dataset.setupTab); });
$('#refresh-statistics').onclick = () => void loadStatistics();
$('#application-language').onchange = (event) => setInterfaceLanguage(event.target.value);
$('#clip-editor-close').onclick = () => setClipEditorOpen(false);
$('#clip-editor-toggle').onclick = () => setClipEditorOpen(true);
document.querySelectorAll('[data-editor-tab]').forEach((button) => { button.onclick = () => setEditorTab(button.dataset.editorTab); });
setClipEditorOpen(false);
organizeSetupCards();
const interfaceLocalizer = new MutationObserver((changes) => {
  // Text changes such as the status label happen often.  Translating the
  // complete page for each one caused visible UI stalls on large libraries.
  // Handle each changed node only; detached child nodes arrive with their
  // parent element when a generated card is appended to the document.
  for (const change of changes) for (const node of change.addedNodes) localizeTree(node);
});
interfaceLocalizer.observe(document.body, { childList:true, subtree:true });
try { setSetupTab(localStorage.getItem('clipfinder-setup-tab') || 'search'); } catch { setSetupTab('search'); }
try { setInterfaceLanguage(localStorage.getItem(APP_LANGUAGE_KEY) || 'en', false); } catch { setInterfaceLanguage('en', false); }
document.addEventListener('keydown', (event) => {
  const quickOpen = $('#quick-review-dialog').open;
  if (quickOpen) {
    if (event.ctrlKey || event.metaKey || event.altKey || event.target.matches('input, textarea, select')) return;
    const key = event.key.toLowerCase();
    if (key === 'a') { event.preventDefault(); rateQuickClip('accepted'); }
    else if (key === 'r') { event.preventDefault(); rateQuickClip('rejected'); }
    else if (event.key === 'ArrowLeft') { event.preventDefault(); moveQuickReview(-1); }
    else if (event.key === 'ArrowRight') { event.preventDefault(); moveQuickReview(1); }
    else if (event.key === 'Escape') { event.preventDefault(); closeQuickReview(); }
    return;
  }
  if (event.key === 'Escape') setSetupSidebar(false);
});
$('#close-dialog').onclick = () => $('#video-dialog').close();
$('#video-dialog').addEventListener('close', clearFullRecordingPreview);
let dashboardPollTimer = null;
let runtimePollTimer = null;
function dashboardPollDelay() { return state.hasActiveVideoJobs || state.hasActiveImports ? 2000 : 15000; }
function scheduleDashboardPoll(delay = dashboardPollDelay()) {
  if (dashboardPollTimer) window.clearTimeout(dashboardPollTimer);
  dashboardPollTimer = window.setTimeout(runDashboardPoll, delay);
}
async function runDashboardPoll() {
  dashboardPollTimer = null;
  if (!document.hidden) await refreshDashboard(false);
  scheduleDashboardPoll();
}
function scheduleRuntimePoll(delay = 300000) {
  if (runtimePollTimer) window.clearTimeout(runtimePollTimer);
  runtimePollTimer = window.setTimeout(runRuntimePoll, delay);
}
async function runRuntimePoll() {
  runtimePollTimer = null;
  if (!document.hidden) {
    try { await loadRuntimeStatus(); } catch { /* Dashboard reconnect status handles API failures. */ }
  }
  scheduleRuntimePoll();
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { stopCaptionPreviewAnimation(); stopLayoutPreview(); }
  else {
    scheduleDashboardPoll(0);
    scheduleRuntimePoll(0);
    if ($('#setup-sidebar')?.classList.contains('open') && document.querySelector('[data-setup-panel="global"]')?.classList.contains('active')) renderCaptionPreview();
  }
});
api('/health')
  .then(async () => { await Promise.all([refreshDashboard(true), loadRuntimeStatus()]); void checkForStartupUpdate(); })
  .catch(() => message('Local API unavailable', true))
  .finally(() => { scheduleDashboardPoll(4000); scheduleRuntimePoll(); });
