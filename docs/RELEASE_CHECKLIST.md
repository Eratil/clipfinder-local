# ClipFinder release checklist

This checklist is for a published Windows release. It does not replace normal
development tests and does not authorize building an installer automatically.

## 1. Prepare the source

1. Set the intended version in `app/version.py`.
2. Review `git status` and remove no user files. A published build must use a
   clean worktree; commit the intended release changes first.
3. Run the complete test suite with 64-bit Python 3.11:

   ```powershell
   python -m pip install -r requirements-test.txt
   python -m pytest -q
   ```

4. Run `git diff --check` and verify PowerShell/JavaScript syntax.
5. If scoring, tags or candidate selection changed, evaluate the quality
   benchmark described in `docs/QUALITY_BENCHMARK.md`. Never commit a real
   benchmark export from `data/benchmarks/`.

## 2. Build only on explicit request

Install Inno Setup 6, then run the build from the repository root:

```powershell
$version = "0.1.20" # example; must equal app/version.py
.\Build-Installer.ps1 -Version $version
```

The script creates a clean `.build-venv`, resolves only exact pinned packages,
runs `pip check`, validates `runtime-compatibility.json`, builds from the
tracked PyInstaller specs, rejects CUDA DLLs in the CPU base package, imports
the packaged backend in a disposable data directory and creates release
provenance in `build-info.json`.

Do not use `-AllowDirtyTree` for a public build. Do not rebuild the GPU add-on
unless `installer/runtime-compatibility.json` deliberately changes its GPU
contract or `gpu_addon_version`.

## 3. Inspect generated artifacts

The `installer-output` directory should contain:

- `ClipFinder-Setup-<version>.exe`;
- `ClipFinder-manifest-<version>.json`;
- optionally `ClipFinder-patch-<previous>-to-<version>.zip` when a compatible
  previous release baseline exists.

Keep `release-cache/ClipFinder-files-<version>.zip` local. It is the compressed
baseline used to create the next direct patch and must not be uploaded or
committed.

## 4. Test the artifact

1. Install over the previous public version and confirm that ClipFinder closes,
   updates and reopens.
2. Confirm that existing recordings, reviews, presets and `%LOCALAPPDATA%\ClipFinder\data`
   remain intact.
3. Confirm CPU analysis on a machine without the GPU add-on.
4. If the GPU contract changed, install the add-on on an NVIDIA test machine
   and confirm the header reports GPU-ready transcription after a real analysis.
5. Test one compact update from the exact predecessor and one full-installer
   fallback from an older version.
6. Open the diagnostic report and verify the version, configured runtime and
   build provenance.

## 5. Publish the GitHub Release

1. Create a public release tagged exactly `v<version>`.
2. Upload the setup EXE and release manifest.
3. Upload the patch ZIP if one was generated.
4. Add useful release notes; ClipFinder displays them in its update panel.
5. Do not attach the local release cache. Publish the GPU add-on only when its
   independent compatibility version changed.
6. From the previous installed version, use **Check for updates** and complete
   one final update test.

If an artifact check fails, do not overwrite an existing release asset. Fix the
source, increment the version and create a new release so hashes and provenance
remain unambiguous.
