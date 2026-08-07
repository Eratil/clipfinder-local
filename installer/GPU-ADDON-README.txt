ClipFinder NVIDIA GPU Add-on

Install this add-on only on a Windows x64 computer with an NVIDIA GPU.
Install the normal ClipFinder application first. The add-on verifies its
packaged CTranslate2 runtime and cannot configure a missing base application.

It checks whether a matching CUDA 12 and cuDNN 9 pair is already installed.
Missing components are installed, then the exact pair is tested with
ClipFinder's packaged CTranslate2 runtime. It accelerates transcription;
similarity search remains CPU-only in the base application.
The add-on asks once for administrator permission at startup, because NVIDIA
components are installed into Program Files.

Keep every .bin file from this package in the same folder as
ClipFinder-GPU-Addon-<version>.exe until setup completes.

The add-on has its own compatibility version. Do not reinstall or rebuild it
for every ClipFinder application update unless the CUDA/cuDNN contract changes.

CUDA 13 alone is not compatible with this contract. A complete supported CUDA
12 installation and a complete cuDNN 9 package for the same CUDA minor version
must be available. cuDNN may remain in NVIDIA's separate CUDNN directory; its
DLL files no longer need to be copied into the CUDA folder.

After setup, open ClipFinder and check the header. LOCAL / GPU READY means the
transcription runtime passed. LOCAL / CPU FALLBACK means ClipFinder remains
usable but the add-on did not produce a working GPU pair. In that case open
Options -> Diagnostic report and check
%LOCALAPPDATA%\ClipFinder\setup-status.txt before reinstalling components.

If this add-on is not installed, ClipFinder still works in CPU mode. It will be slower,
but no recordings or application settings are lost.
