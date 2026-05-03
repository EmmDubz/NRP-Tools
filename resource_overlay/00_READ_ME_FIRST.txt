NirvaliStat resource overlay - which file to double-click
============================================================

TWO BATCH FILES (this folder)
-----------------------------
  Open_resource_overlay_GUI.bat
       Starts deposit_tuner_gui.py — political colours, lassos, Save config, Run analyze
       (writes output/results.* only; run the second batch if you need commodity markdown).

  Run_resource_overlay_analyze_and_commodity_md.bat
       Slow step: overlay CSV/JSON from maps + config.yaml
       Then fast step: rebuild cycles/IRP_2008/PROVISIONAL_commodity_view.md
       Optional prompt: nation YAML fragment (--nations-yaml) for a one-off run; Enter = skip.

PREREQUISITES
-------------
  - Python 3.10+ installed (Make sure to check "Add Python to PATH" during install)
  - The .bat files will automatically install all required packages on first run!
  - maps\Political Map.png and maps\Resource Map aligned.png + config.yaml

More detail: README.md in this folder.
