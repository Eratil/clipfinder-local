# ClipFinder Local

Local app for finding short clips in long MP4 recordings. It keeps videos, models, indexes and results on your computer. NVIDIA acceleration is optional: transcription uses a supported CUDA setup when available and otherwise runs on CPU; similarity search in the distributed base package is CPU-only.

## Start the app

After installing the Windows package, open **ClipFinder** from the Start menu or its desktop shortcut. For a source checkout, double-click `Start-ClipFinder-Desktop.cmd`; `Start-ClipFinder.cmd` runs the local web server at `http://127.0.0.1:8000` and keeps its console visible for development diagnostics.

The original launcher automatically restarts the web server after a crash. The desktop window starts the server itself when needed. Analysis and reference imports use durable local queues: after an unexpected stop, unfinished work is recovered on the next start. Keep ClipFinder open when you want processing to continue without interruption.

## GitHub backup (recommended)

The included `.gitignore` deliberately excludes recordings, exports, the local database, reference clips, Python environments and `.env`. A private repository is fine as a source-code backup. The repository configured in `UPDATE_REPOSITORY` for the installed app's unauthenticated updater must be public; a private update repository needs a separate authenticated service. Review every commit before pushing so personal media or configuration is never added.

After Git for Windows is installed and you have created an empty private GitHub repository, run the following in this folder (replace the URL with your repository URL):

```powershell
git init
git add .
git commit -m "Initial ClipFinder Local version"
git branch -M main
git remote add origin https://github.com/YOUR-ACCOUNT/clipfinder-local.git
git push -u origin main
```

For later backups use `git add .`, `git commit -m "Describe the change"`, then `git push`. Do not add `.env`, `data/` or any video files manually.

## Windows tester installer

`Build-Installer.ps1` creates a Windows x64 installer in `installer-output`. It requires Git, internet access, 64-bit Python 3.11 and Inno Setup 6. It uses a PyInstaller one-folder build (faster and more reliable than a single self-extracting executable for AI dependencies). The packaged app already includes Python and ClipFinder's Python packages.

On the build computer, install Inno Setup 6, then run:

```powershell
$version = "0.2.0" # must match app/version.py
.\Build-Installer.ps1 -Version $version
```

A release build must come from a clean Git worktree. The script creates a fresh `.build-venv`, installs the exact versions from the lock/constraint files, verifies the CPU-only base package and performs a packaged smoke test. `-AllowDirtyTree` exists only for local experiments and must not be used for a published release.

On the first installation, the setup includes Microsoft Visual C++ Redistributable and `Configure-ClipFinder.ps1` checks FFmpeg and Microsoft Edge WebView2 Runtime, using `winget` when either is missing. The script retains fallback repair paths for Visual C++ as well. It writes a per-user runtime profile; later application updates preserve that profile instead of silently changing CPU/GPU choices. The tester needs internet access for missing system components and for the first pinned Whisper model download.

To create the separate NVIDIA GPU add-on containing CUDA 12.9.2 and cuDNN 9.24 installers placed in this project's parent `outputs` folder, run:

```powershell
.\Build-Installer.ps1 -GpuAddon -GpuAddonVersion 1.0.0
```

The GPU add-on is intentionally split by Inno Setup into a setup executable plus `.bin` data files. Keep those files together; send the whole GPU add-on folder in one ZIP. Its version follows `installer/runtime-compatibility.json`, independently from the application version, so rebuild it only when the CUDA/cuDNN/CTranslate2 contract changes. It matches CUDA and cuDNN by minor version and runs a real packaged CTranslate2 probe before enabling GPU transcription. Similarity search remains CPU-only in the base package. Do not publish the add-on as the automatic-update asset. Data stays in `%LOCALAPPDATA%\ClipFinder\data`. CUDA/cuDNN redistribution and all third-party components still require a licence audit before commercial distribution.

## Updating the desktop app

You do **not** need to uninstall ClipFinder or remove its local data before an update. Each installer uses the same Windows application identity and install directory. Close ClipFinder, then run the newer `ClipFinder-Setup-x.y.z.exe`; it updates the program in place and keeps recordings, results, settings and exports in `%LOCALAPPDATA%\ClipFinder\data`.

The **Options** tab has a manual **Check for updates** button. ClipFinder also performs one quiet check at startup; a failed check is ignored, while an available release shows a small badge. After confirmation, ClipFinder requires an exact-version asset and GitHub-provided SHA-256 digest, downloads either the direct compact patch or the full setup fallback, closes itself and restarts after the verified update. Data in `%LOCALAPPDATA%\ClipFinder\data` is never part of an update.

To publish an update (the complete checklist is in [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md)):

1. Change the version in `app/version.py`.
2. Commit and push the code.
3. Build the matching installer with `./Build-Installer.ps1 -Version <version>` (replace `<version>` with the numeric value, without `v`).
4. Create a public GitHub Release tagged `v<version>` and upload the matching `ClipFinder-Setup-<version>.exe` and `ClipFinder-manifest-<version>.json` from `installer-output`.
5. If the build created `ClipFinder-patch-<previous>-to-<version>.zip`, upload it too. Only the exact previous version uses that smaller patch; every other version safely falls back to the full setup EXE.

The build keeps one compressed local baseline in `release-cache/` (ignored by Git) so it can generate the next patch. It replaces the older baseline after a successful build, keeping disk use bounded. Do not upload this cache to GitHub.

The app can check releases only when the repository and release are public. A private repository would require a separate authenticated update service; do not embed a GitHub access token in the installed application.

## First-time installation

Source-development requirements: 64-bit Python 3.11 and FFmpeg/FFprobe in PATH. NVIDIA is optional. GPU transcription additionally requires one supported, matching CUDA 12/cuDNN 9 pair; the base installer works on CPU.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python scripts\doctor.py
```

Run `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000` if you prefer manual startup instead of the launcher.

On a new Windows computer, the simpler option is to double-click `Install-ClipFinder.cmd`. It installs Python 3.11 and FFmpeg through `winget` when they are missing, creates `.venv`, installs the app packages and runs the diagnostic. It cannot safely automate CUDA/cuDNN because those depend on the NVIDIA driver. For a computer without NVIDIA GPU support, run `Install-ClipFinder.cmd -CpuFallback` from PowerShell.

## Main workflow

1. Upload an MP4 and wait for its progress bar to reach 100%.
2. Or paste a public YouTube link or Twitch VOD URL. ClipFinder downloads it locally first, then analyzes it like an uploaded file. Only download material you are allowed to use.
3. Choose **Fast** for a targeted first pass, **Default** for the normal workflow or **Extended** for additional completeness, reading and context checks on the strongest candidates.
4. Use exact-text search for words that occur in the transcript, or description search for requests like `a funny reaction to surprising news`.
5. Create a reference collection and add good candidates with **Add as reference**. You can also import finished clips from a local folder.
6. A temporary public Short/video preview can save only its derived semantic fingerprint to a discovery pattern set; the temporary audio and frame are removed after analysis.
7. Use **Best of stream** to build a diversified shortlist across a whole recording. **Quick selection** is the keyboard-driven review window for rapidly approving or rejecting an already ranked list.
8. Review assigned tags as correct or incorrect and approve/reject clips. Those revision-bound decisions train the selected discovery profile without silently attaching an old decision to changed reanalysis results.

## Chat reaction analysis

After selecting a recording, use **Chat reaction analysis** to import a `.json`, `.jsonl`, `.ndjson`, `.csv`, `.tsv` or `.txt` transcript of the live chat. The file must contain relative timestamps from the beginning of the recording, for example `01:23:45` or numeric seconds; JSON exports using Twitch's `content_offset_seconds` are supported too. An ISO wall-clock field such as `timestampUtc` alone is not enough unless the logger also writes a relative offset. Set the expected delay between what happens on stream and the chat response (start with `6` seconds, then correct it if the displayed messages appear too early or too late).

ClipFinder compares the message burst after each spoken clip with the recent background activity of the chat. It boosts only clear bursts, shows message count and distinct chatters in the ranking, and displays a few relevant chat lines below the candidate. Chat content remains in the local application database.

## Separate microphone and sound analysis

In **Saved setup → Global → Analysis audio sources**, choose either the original single-track workflow or split processing. In split mode, captions, text search and prompts use only the microphone track; optional all-sounds and game tracks are scanned for dynamic sound events that improve clip ranking without adding game dialogue to the transcript. The default mapping is Track 1 = all sounds, Track 2 = microphone, Track 3 = game. These settings apply when a recording is newly analyzed or reanalyzed.

## Reliability notes

- The browser page can be closed once uploading is complete; do not close the launcher window.
- After an unexpected shutdown, leased work is safely returned to the durable queue and recovered after restart. A completed analysis is never replaced by a failed reanalysis.
- The page refreshes active work about every two seconds and backs off to about fifteen seconds while idle. API documentation is available at `/docs`.
- CUDA 13 is not sufficient for the pinned CTranslate2 runtime: it needs `cublas64_12.dll`. Install a supported CUDA 12 version next to CUDA 13 and matching cuDNN 9 for the same CUDA minor. cuDNN may stay in NVIDIA's standalone folder; copying DLLs into CUDA is no longer required. `doctor.py` uses the same semantic pair resolver as the app. For a slow fallback, set `WHISPER_DEVICE=cpu` and `WHISPER_COMPUTE_TYPE=int8` in `.env`, then restart.
- The Recordings header shows source recordings, exported clips and the small review-audio archive separately. **Remove source video** deletes only the large managed source file. It keeps analysis runs, transcripts, embeddings, chat data, tags, decisions and model-training feedback; every moment carrying human feedback also keeps a revision-specific MP3 for listening. Reanalysis, full-recording preview and MP4 export are unavailable after source removal.
- Database schema upgrades are transactional and versioned. An older ClipFinder refuses to modify a library already upgraded by a newer version instead of attempting an unsafe downgrade.

## Local data locations

The installed application stores its database and managed files under `%LOCALAPPDATA%\ClipFinder\data`: recordings in `incoming`, exports in `exports`, reviewed audio in `review-audio`, reference work in `reference-downloads`, stage cache in `cache`, temporary work/locks in `work` and diagnostics in `logs`. Runtime selection is in `%LOCALAPPDATA%\ClipFinder\runtime.json`; post-install status is in `%LOCALAPPDATA%\ClipFinder\setup-status.txt`. Model snapshots use the normal Hugging Face cache outside the application data folder. Updating or uninstalling ClipFinder does not silently delete these user-owned data or model caches. A source checkout uses its local `./data` directory unless `CLIPFINDER_DATA_DIR` is set.

## Technical documentation

- [Architecture and persistent data model](docs/ARCHITECTURE.md)
- [Troubleshooting and diagnostic reports](docs/TROUBLESHOOTING.md)
- [Release checklist](docs/RELEASE_CHECKLIST.md)
- [Quality benchmark](docs/QUALITY_BENCHMARK.md)
