# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**PiFinder™** is an open-source plate-solving telescope finder based on Raspberry Pi. It combines camera-based star pattern recognition, GPS positioning, IMU motion tracking, and a custom UI to provide zero-setup telescope pointing and observation logging. The project includes hardware designs (PCB schematics, 3D-printable cases), firmware/software, and comprehensive documentation.

**Current Version:** 2.3.0

## Development Commands

**Development workflow uses Nox for task automation:**
```bash
nox -s lint          # Code linting with Ruff (auto-fixes issues)
nox -s format        # Code formatting with Ruff
nox -s type_hints    # Type checking with MyPy
nox -s smoke_tests   # Quick functionality validation
nox -s unit_tests    # Full unit test suite
nox -s babel         # I18n message extraction and compilation
```

**Direct testing with pytest:**
```bash
pytest -m smoke      # Smoke tests for core functionality
pytest -m unit       # Unit tests for isolated components
pytest -m integration # End-to-end integration tests
```

**Development setup:**
```bash
cd python/
python3.9 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements_dev.txt
```
If the .venv dir already exists, you can directly source it and run the app.

**Running the application:**
Development setup has to have run and you should be in .venv virtual environment
```bash
cd python/
python -m PiFinder.main [options]
```
Typical development startup (with fake hardware, local keyboard, and debug camera):
```bash
python3.9 -m PiFinder.main -fh --camera debug --keyboard local -x
```

**Pre-commit hooks:**
The repository uses pre-commit hooks for automated testing before commits:
```bash
pre-commit install  # Set up hooks
pre-commit run --all-files  # Run manually
```
Configured checks: `type_hints` and `smoke_tests` via Nox

## Architecture Overview

**Multi-Process Design:** PiFinder uses a process-based architecture where each major subsystem runs in its own process, communicating via queues and shared state objects:

- **Main Process** (`main.py`) - UI event loop, menu system, user interaction
- **Camera Process** (`camera_interface.py`, `camera_pi.py`, `camera_debug.py`, `camera_none.py`) - Image capture from various camera types with auto-exposure support
- **Solver Process** (`solver.py`) - Plate solving using **Cedar Detect/Cedar Solve** libraries for star pattern recognition (binaries in `bin/`)
- **GPS Process** (`gps_gpsd.py`, `gps_ubx.py`, `gps_fake.py`) - Location/time via GPSD daemon or direct UBlox protocol with configurable baud rates
- **IMU Process** (`imu_pi.py`, `imu_fake.py`) - Motion tracking with BNO055 sensor, includes configurable threshold scaling and degraded operation mode if IMU unavailable
- **Integrator Process** (`integrator.py`) - Combines solver + IMU data for real-time positioning
- **Web Server Process** (`server.py`) - Web interface and SkySafari telescope control integration
- **Position Server Process** (`pos_server.py`) - External protocol support for telescope control
- **SQM Process** (`sqm/`) - Sky Quality Meter functionality for measuring sky darkness (experimental)

**State Management:**
- `SharedStateObj` (`state.py`) - Process-shared state using multiprocessing managers
- `UIState` - UI-specific state management with real-time updates
- `SharedStateObjProxy` - Thread-safe proxy for cross-process communication
- Real-time synchronization of telescope position, GPS coordinates, and solved sky coordinates

**Database Layer:**
- SQLite backend (`astro_data/pifinder_objects.db`)
- `ObjectsDatabase` (`db/objects_db.py`) - Astronomical catalog management
- `ObservationsDatabase` (`db/observations_db.py`) - Session logging and observation tracking
- `CatalogBuilder`, `CatalogFilter` (`catalogs.py`) - Dynamic catalog filtering and object selection
- Modular catalog import system (`catalog_imports/`) supporting NGC, IC, Messier, Caldwell, Herschel, SAC, WDS double stars, Arp peculiar galaxies, Sharpless HII regions, bright stars, and comets

**Hardware Abstraction:**
- Camera interface supporting IMX296 (global shutter), IMX290/462, HQ cameras with auto-exposure
- Display system (`displays.py`) for SSD1351 OLED and ST7789 LCD with red-light preservation
- Hardware keypad with PWM brightness control via `rpi-hardware-pwm`
- GPS integration via GPSD daemon or direct UBlox protocol with configurable baud rates
- IMU sensor integration (BNO055) for motion detection with configurable sensitivity and fallback modes

## Key Directories and Modules

**Core Application (`python/PiFinder/`):**
- `main.py` - Application entry point and UI event loop
- `solver.py`, `integrator.py` - Plate solving and position integration
- `camera_*.py` - Camera interface implementations (Pi, ASI, debug, none)
- `gps_*.py` - GPS implementations (GPSD, UBlox, fake)
- `imu_*.py` - IMU implementations (Pi hardware, fake)
- `displays.py` - Display abstraction for OLED/LCD
- `keyboard_interface.py` - Input handling
- `config.py` - Configuration management
- `catalogs.py`, `catalog_base.py`, `composite_object.py` - Catalog system
- `calc_utils.py` - Astronomical calculations and coordinate transformations
- `equipment.py` - Telescope and eyepiece configuration
- `auto_exposure.py` - Camera auto-exposure algorithms
- `i18n.py` - Internationalization support

**UI Components (`python/PiFinder/ui/`):**
- `menu_manager.py`, `menu_structure.py` - Menu system and navigation
- `chart.py` - Star chart rendering
- `object_list.py`, `object_details.py` - Object catalog browsing
- `console.py` - Debug console
- `align.py` - Telescope alignment screens
- `preview.py` - Camera preview display
- `log.py` - Observation logging interface
- `gpsstatus.py` - GPS status display
- `sqm.py` - Sky Quality Meter UI
- `radec_entry.py` - RA/Dec coordinate entry
- `equipment.py`, `location_list.py`, `software.py` - Configuration UIs
- `marking_menus.py` - Contextual marking menus

**Database Layer (`python/PiFinder/db/`):**
- `objects_db.py` - Astronomical object database interface
- `observations_db.py` - User observation logging
- `db.py`, `db_utils.py` - Database utilities

**Catalog Imports (`python/PiFinder/catalog_imports/`):**
- Modular loaders for NGC/IC, Messier, Caldwell, Herschel, SAC, WDS, Arp, Sharpless, bright stars, comets
- `post_processing.py` - Catalog data normalization and enhancement

**SQM Module (`python/PiFinder/sqm/`):**
- Sky Quality Meter for measuring sky darkness (experimental feature)

**Testing (`python/tests/`):**
- Test suite with pytest markers: `smoke`, `unit`, `integration`
- Tests for calculations, catalogs, menu structure, multiprocess logging, auto-exposure, etc.

**Data and Assets:**
- `astro_data/` - Astronomical catalogs and object databases (NGC/IC, WDS, Arp, Sharpless, etc.)
- `fonts/` - UI fonts
- `images/` - Application images and icons
- `help/` - Context-sensitive help content
- `markers/` - Image pattern markers for calibration
- `test_images/` - Test images for development
- `views/` - Web interface templates (HTML/CSS/JS)
- `locale/` - Translations (French, German, Spanish)

**Hardware and Documentation:**
- `case/` - 3D printable enclosure files (STL, OpenSCAD)
- `kicad/`, `gerbers/` - PCB design files and manufacturing outputs
- `docs/` - Comprehensive documentation (build guides, user manuals, developer guide)
- `bin/` - Precompiled Cedar Detect/Solve binaries for ARM platforms
- `scripts/` - Utility scripts and test scenarios

## Configuration

**Config Files:**
- `default_config.json` - System defaults with comprehensive settings for all hardware and UI components
- `~/PiFinder_data/config.json` - User-specific runtime settings (created on first run)
- Configuration includes:
  - Display settings (brightness, orientation, screen timeout)
  - Camera parameters (exposure, gain, auto-exposure behavior)
  - GPS settings (type: GPSD/UBlox, baud rate)
  - IMU threshold scaling for sensitivity adjustment
  - Chart rendering options (RA/Dec grid, DSO brightness, reticle, constellations)
  - Equipment profiles (telescopes, eyepieces with detailed optical specifications)
  - Catalog filters (object types, constellations, catalog sources)
  - UI preferences (animation speed, text scrolling, menu behavior)
  - Mount type (Alt/Az or Equatorial)

**Equipment Profiles:**
- Telescope configuration: aperture, focal length, obstruction percentage, mount type, image orientation
- Eyepiece configuration: focal length, AFOV, field stop
- Multiple profiles supported with active selection indices

**Hardware Configuration Options:**
- **Camera**: Pi Camera (IMX296/290/462, HQ), ASI cameras, debug mode (simulated images), none
- **Display**: SSD1351 OLED or ST7789 LCD with brightness control and orientation settings
- **Input**: Hardware keypad, local keyboard (development), web interface
- **GPS**: GPSD daemon, direct UBlox protocol with configurable baud rates (9600 default), fake (testing)
- **IMU**: BNO055 sensor with configurable threshold scaling, fake (testing), degraded operation mode

## Testing Strategy

Tests use pytest with custom markers for different test levels:
- `@pytest.mark.smoke` - Quick validation of core functionality (run in pre-commit hooks)
- `@pytest.mark.unit` - Isolated component testing
- `@pytest.mark.integration` - End-to-end multi-process workflows

**Test Files:**
- `test_calc_utils.py` - Astronomical calculations and coordinate transformations
- `test_catalog_data.py` - Catalog data validation and integrity
- `test_menu_struct.py` - Menu structure and navigation logic
- `test_multiproclogging.py` - Multi-process logging system
- `test_auto_exposure.py` - Camera auto-exposure algorithms
- `test_sqm.py` - Sky Quality Meter functionality
- `test_radec_entry.py` - RA/Dec coordinate entry validation
- `test_sys_utils.py` - System utilities
- `test_main.py` - Main application integration tests

**Testing Best Practices:**
- Run smoke tests before commits via pre-commit hooks
- Use `nox -s smoke_tests` for quick validation during development
- Run full unit tests (`nox -s unit_tests`) before submitting PRs
- Test with fake hardware modules (`-fh` flag) for development without physical devices

## Code Quality and Conventions

**Linting and Formatting:**
- **Linter:** Ruff v0.4.8 with auto-fix enabled
- **Formatter:** Ruff (Black-compatible)
- **Target:** Python 3.9+
- **Line Length:** 88 characters
- **String Quotes:** Double quotes preferred
- **Indentation:** Spaces (no tabs)

**Type Checking:**
- **Tool:** MyPy with gradual typing adoption
- Run via `nox -s type_hints`
- Install types automatically with `--install-types --non-interactive`

**Internationalization (I18n):**
- **Framework:** Babel for message extraction and compilation
- **Supported Languages:** English (default), French, German, Spanish
- **Workflow:**
  - Extract translatable strings: `nox -s babel`
  - Translations stored in `python/locale/{lang}/LC_MESSAGES/`
  - Use `_()` function for translatable strings in code
- **TRANSLATORS comments:** Special comments extracted for translator context

**Development Environment:**
- Python 3.9 required (targeting Raspberry Pi OS compatibility)
- Virtual environment strongly recommended (`.venv/`)
- Environment variables: Set `OPENBLAS_NUM_THREADS=1` and `MKL_NUM_THREADS=1` for Skyfield performance

**Code Organization:**
- Process-based architecture requires careful queue and state management
- Use `MultiprocLogging` for consistent logging across processes
- Hardware abstraction layers allow fake implementations for testing
- Configuration loaded from JSON files; avoid hardcoded values

**Recent Development Focus (v2.3.0):**
- Experimental SQM (Sky Quality Meter) functionality
- Camera auto-exposure improvements with configurable zero-star handling
- GPS source flexibility (GPSD vs UBlox) with configurable baud rates
- IMU degraded operation mode for graceful fallback
- WDS double star catalog fixes and improvements
- Comet catalog enhancements
- Case and hardware refinements (dovetail design, tolerances)

**External Resources:**
- Official documentation: [pifinder.readthedocs.io](https://pifinder.readthedocs.io/en/release/)
- Discord community: [PiFinder Discord](https://discord.gg/Nk5fHcAtWD)
- Project website: [PiFinder.io](https://www.pifinder.io/)
- Cedar libraries: [Cedar Detect](https://github.com/smroid/cedar-detect) and [Cedar Solve](https://github.com/smroid/cedar-solve) (used with permission)
