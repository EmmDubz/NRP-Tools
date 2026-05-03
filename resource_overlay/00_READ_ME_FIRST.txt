Resource overlay — which file to run first
============================================

This folder is part of the NRP-Tools repository unless you copied it alone.

TWO BATCH FILES (Windows, in this folder)
-----------------------------------------
  1_Run_GUI.bat
       Starts deposit_tuner_gui.py — political colours, lassos, save config, run analysis from the GUI.
       (Writes output under output/; use the second batch if you want the full analysis + commodity markdown pipeline from the command line.)

  2_Run_Analysis.bat
       Runs analyze_resources.py then build_commodity_view_md.py using maps/ and config.yaml.
       Optional prompt: nation YAML fragment (--nations-yaml) for a one-off run; press Enter to skip.

PREREQUISITES
-------------
  - Python 3.10+ on PATH ("Add Python to PATH" during Windows install).
  - The .bat files create scripts\.venv and install dependencies on first run.
  - config.yaml (copy from config.example.yaml) and the map PNG paths it references.

More detail: README.md in this folder.
