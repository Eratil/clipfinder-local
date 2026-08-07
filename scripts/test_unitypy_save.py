"""Verify that UnityPy can save a modified copy of this game's string table.

This deliberately writes only to the local build directory, never to the
Steam installation.
"""

from pathlib import Path
import UnityPy


GAME_BUNDLE = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\Agatha Christie - Death on the Nile"
    r"\Agatha Christie - Death on the Nile_Data\StreamingAssets\aa\StandaloneWindows64"
    r"\localization-string-tables-english(en)_assets_all.bundle"
)
OUTPUT_DIRECTORY = Path("build")


def main() -> None:
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    environment = UnityPy.load(str(GAME_BUNDLE))
    for obj in environment.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        asset = obj.read()
        if asset.m_Name == "SystemStrings_en":
            asset.m_TableData[0].m_Localized = "Rozmowa towarzyska"
            asset.save()
            break
    else:
        raise RuntimeError("Nie odnaleziono tabeli SystemStrings_en")

    environment.save(out_path=str(OUTPUT_DIRECTORY))
    print((OUTPUT_DIRECTORY / GAME_BUNDLE.name).resolve())


if __name__ == "__main__":
    main()
