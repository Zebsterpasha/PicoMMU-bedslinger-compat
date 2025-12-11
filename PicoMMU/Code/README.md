# What's been done:
- Fixed bugs:
    - heating was turned off if a filament change was required at the start of a print
    - PRINT_END was fixed to ensure the print completes correctly
- Added temperature recovery before RESUME
- Changed the print return sequence for bed_slinger (first return to the desired Z height, and only then XY)
- Added Z parking point
- Added pregate (and actually postgear) sensor configuration
- Added a service for automatically loading filament to the HUB via the pregate sensor

To use the automatic filament loading service:
- Add a file pregate_autoload.py to /home/*USER*/klipper/klippy/extras/
- Add an import "from . import pregate_autoload" to the file /home/*USER*/klipper/klippy/extras/__init__.py
