# GEMINI.md: Project zhou-v2

This document provides a comprehensive overview of the `zhou-v2` project, its architecture, and instructions for running and development. It is intended to be used as a primary context source for AI-assisted development.

## 1. Project Overview

`zhou-v2` is a high-performance, multi-process automation framework for Python, designed to interact with GUI applications, specifically the MuMu Android emulator. Based on the context (e.g., "deployment plans", "cost bar"), it appears to be an automation tool for the game "Arknights".

The framework is built around a pipeline of specialized processes that communicate via high-speed Inter-Process Communication (IPC) using shared memory. This design allows for precise, frame-perfect execution of pre-defined action plans.

### Core Architecture

The system is composed of three main concurrent processes, orchestrated by the main script `run.py`:

1.  **Perception (`CaptureProcess`):**
    *   **Source:** `app/perception/capture_process.py`
    *   **Engine:** `app/perception/engines/mumu.py`
    *   **Responsibility:** Continuously captures the screen of the MuMu emulator at high speed and writes the raw image data into a `TripleSharedBuffer`.

2.  **Analysis (`RulerProcess`):**
    *   **Source:** `app/analysis/ruler_process.py`
    *   **Responsibility:** Reads image data from the `TripleSharedBuffer`, analyzes it to track game state (e.g., calculating the current frame number), and writes this metadata into a `DoubleSharedBuffer`.

3.  **Control (`CommanderProcess`):**
    *   **Source:** `app/control/commander_process.py`
    *   **Responsibility:** The "brain" of the operation. It reads the target action plan from a `.yaml` file, reads the current frame number from the `DoubleSharedBuffer`, and when the target frame is reached, it sends precise commands (clicks, drags) to the emulator via a controller adapter (e.g., `MumuMacroController`). It features a state machine to precisely step through frames to execute actions at the exact target frame.

### Key Concepts

*   **IPC Buffers:** Custom shared memory buffers (`TripleSharedBuffer` for images, `DoubleSharedBuffer` for frame data) are used for extremely fast, lock-free data exchange between processes. This is critical for performance.
*   **Action Plans:** YAML files located in the `/plans` directory that define a sequence of actions to be executed at specific `trigger_frame`s.
*   **Modes of Operation:** The `run.py` script provides three main functionalities:
    *   `run`: Execute a given plan.
    *   `record`: Record user actions in the emulator to generate a new plan file.
    *   `calibrate`: Run a calibration routine, likely to detect the position and state of UI elements like the "cost bar".

## 2. Building and Running

### Dependencies

The project's dependencies are listed in `pyproject.toml` and managed by `uv`.

*   **Main Dependencies:** `numpy`, `pillow`, `pydantic`, `pywin32`, `pyyaml`.
*   **Development Dependencies:** `pytest`.

To install dependencies, you would typically use a command like:
```bash
# This is an inferred command, assuming uv is the package manager
uv pip install -r requirements.txt 
# Or more directly if uv is configured for the project
uv sync
```

### Running the Application

The main entry point is `run.py`.

*   **To execute a plan:**
    ```bash
    python run.py run <plan_name>
    ```
    *(Example: `python run.py run deployment-showcase`)*

*   **To record a new plan:**
    ```bash
    python run.py record <new_plan_name>
    ```
    *(This will start the capture and ruler processes and listen for inputs to save into a new `.yaml` file in the `plans` directory. Stop with Ctrl+C.)*

*   **To run calibration:**
    ```bash
    python run.py calibrate
    ```

### Running Tests

The project uses `pytest` for testing. The configuration is located in `pyproject.toml`.

*   **To run all tests:**
    ```bash
    pytest
    ```

*   **To include performance tests:**
    ```bash
    pytest --run-performance
    ```
    *(Note: The marker is defined in `pyproject.toml`, but the command to run it is inferred. It might be `pytest -m performance`)*

## 3. Development Conventions

*   **Code Structure:** The application logic is well-organized into `app` sub-packages: `perception`, `analysis`, `control`, `core`, and `utils`.
*   **Configuration:** The project uses `pydantic` models for strong typing and validation of configuration, which is loaded from `.yaml` files in the `configs` directory.
*   **Typing:** The codebase uses modern Python type hints extensively.
*   **Logging:** The standard `logging` library is configured in `run.py` and used throughout the modules.
*   **Testing:** Tests are located in the `tests/` directory. They provide a good example of how to interact with the system's components, especially the IPC buffers. `pytest` fixtures are used to manage test resources like configuration and buffers.
