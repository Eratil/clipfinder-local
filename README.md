# ClipFinder Local

Local app for finding short clips in long MP4 recordings. It uses your NVIDIA GPU for transcription and keeps videos, models, indexes and results on your computer.

## Start the app

After the one-time installation, double-click `Start-ClipFinder.cmd`. It opens the local app at `http://127.0.0.1:8000`.

The launcher automatically restarts the web server after a crash. Keep its console window open while an analysis or a reference-folder import is running.

## GitHub backup (recommended)

Keep this repository **private**. The included `.gitignore` deliberately excludes your recordings, exports, local database, reference clips, Python environment and `.env` configuration. That means GitHub stores the application code and installation files, not your stream material or local results.

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

## First-time installation

Requirements: Python 3.11, FFmpeg/FFprobe in PATH and an NVIDIA GPU. GPU transcription additionally requires CUDA 12 cuBLAS and cuDNN 9.

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
3. Use exact-text search for words that occur in the transcript.
4. Use description search for requests like `a funny reaction to surprising news`. This searches by meaning, not exact words.
5. Create a reference collection and add good candidates with **Add as reference**.
6. To use finished clips from another folder, select a collection, enter the full folder path, then choose **Import folder as references**. The app transcribes the reference videos locally and adds their semantic profiles to the collection.
7. Select a recording and use **Find similar to collection**.

## Boss-death report for Soulslikes

In **Saved setup -> Boss deaths**, save a profile once for every game or HUD layout. Add a short (0.25-8 seconds), clean sample of the death sound and a game screenshot with one clear red rectangle around the entire area where the boss name can appear. The app detects that rectangle automatically and stores it as a resolution-independent crop.

Choose the recording audio track that has the clean game sound in the profile or directly before **Analyze deaths** (for example, a separate track without background music). The app finds matches in that selected track, reads the boss label from a frame immediately before each match, and produces a downloadable CSV. Its first rows are the death count per boss; the later rows list every detected death with its timecode and sound-match score. The first run can download EasyOCR's local recognition model.

## Separate microphone and sound analysis

In **Saved setup -> Clip setup -> Analysis audio sources**, choose either the original single-track workflow or split processing. In split mode, captions, text search and prompts use only the microphone track; optional all-sounds and game tracks are scanned for dynamic sound events that improve clip ranking without adding game dialogue to the transcript. The default mapping is Track 1 = all sounds, Track 2 = microphone, Track 3 = game. These settings apply when a recording is newly analyzed or reanalyzed.

## Reliability notes

- The browser page can be closed once uploading is complete; do not close the launcher window.
- After an unexpected server shutdown, an active job is marked `interrupted`. Click **Run analysis again** to start it again from the source video.
- The page refreshes job and import progress every four seconds. API documentation is available at `/docs`.
- CUDA 13 is not sufficient: current faster-whisper builds need `cublas64_12.dll`. Install CUDA 12.x next to CUDA 13; do not rename CUDA 13 DLLs. Place cuDNN 9 DLL files in the CUDA 12 `bin` folder. `doctor.py` automatically locates that folder. For a temporary slow fallback, set `WHISPER_DEVICE=cpu` and `WHISPER_COMPUTE_TYPE=int8` in `.env`, then restart the app.
- The Recordings header shows total source-recording and exported-clip disk use. Finished recordings have a manual **Delete recording** action; it removes the source recording and its analysis data, but keeps exported clips.
