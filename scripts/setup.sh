#!/usr/bin/env bash
# setup.sh
# Cross-platform helper to ensure Python + venv + pip deps are present.
# Supports Ubuntu (Debian) and macOS flows. Safe-by-default; will only
# install system packages if you pass --install-prereqs.
#
# Usage examples:
#   # Dry run (checks only):
#   ./scripts/setup.sh
#
#   # Create venv and install requirements.txt:
#   ./scripts/setup.sh --venv .venv --requirements requirements.txt
#
#   # Automatically install system prerequisites (will use sudo):
#   ./scripts/setup.sh --install-prereqs
#
#   # Force recreate venv (delete existing and recreate)
#   ./scripts/setup.sh --force
#
#   # Specify python executable:
#   ./scripts/setup.sh --python /usr/bin/python3.10
#
set -euo pipefail

# Defaults
VENV_DIR=".venv"
REQUIREMENTS="requirements.txt"
FORCE=false
INSTALL_PREREQS=false
PYTHON_EXE=""
MIN_PY_MAJOR=3
MIN_PY_MINOR=10

progname="$(basename "$0")"

usage() {
  cat <<EOF
Usage: $progname [options]

Options:
  --venv DIR             venv directory (default: .venv)
  --requirements PATH    requirements file to install (default: requirements.txt)
  --python PATH          python executable to use (default: autodetect python3)
  --force                remove existing venv and recreate
  --install-prereqs      install system packages (uses sudo)
  -h, --help             show this help
EOF
}

log() { printf '[%s] %s\n' "$(date +'%H:%M:%S')" "$*"; }
err() { printf '[ERROR] %s\n' "$*" >&2; }

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --venv) VENV_DIR="$2"; shift 2;;
    --requirements) REQUIREMENTS="$2"; shift 2;;
    --python) PYTHON_EXE="$2"; shift 2;;
    --force) FORCE=true; shift;;
    --install-prereqs) INSTALL_PREREQS=true; shift;;
    -h|--help) usage; exit 0;;
    *) err "Unknown arg: $1"; usage; exit 2;;
  esac
done

# Detect platform
UNAME="$(uname -s)"
IS_MAC=false
IS_LINUX=false
if [[ "$UNAME" == "Darwin" ]]; then
  IS_MAC=true
elif [[ "$UNAME" == "Linux" ]]; then
  IS_LINUX=true
else
  err "Unsupported OS: $UNAME"
  exit 1
fi

# On Linux check /etc/os-release for debian/ubuntu family
IS_UBUNTU=false
if $IS_LINUX && [[ -f /etc/os-release ]]; then
  . /etc/os-release
  if [[ "${ID:-}" =~ (ubuntu|debian) || "${ID_LIKE:-}" =~ (debian) ]]; then
    IS_UBUNTU=true
  fi
fi

# Helper: find python
find_python() {
  if [[ -n "$PYTHON_EXE" ]]; then
    echo "$PYTHON_EXE"
    return 0
  fi

  # Prefer python3.10+, then python3, then python
  for p in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$p" >/dev/null 2>&1; then
      echo "$p"
      return 0
    fi
  done

  return 1
}

# Check python version
check_python_version() {
  local py="$1"
  if ! "$py" -c "import sys; exit(0 if (sys.version_info.major>=$MIN_PY_MAJOR and sys.version_info.minor>=$MIN_PY_MINOR) else 1)"; then
    return 1
  fi
  return 0
}

# Install system prerequisites (only if user passed --install-prereqs)
install_prereqs() {
  if $IS_MAC; then
    log "macOS detected."
    if ! command -v brew >/dev/null 2>&1; then
      log "Homebrew not found. Attempting to install Homebrew..."
      /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
      log "Homebrew installed (or attempted)."
    fi
    log "Installing python3 via brew if missing..."
    brew install python@3.12 || brew install python || true
  elif $IS_UBUNTU; then
    log "Ubuntu/Debian detected. Installing python3 and venv packages via apt (requires sudo)..."
    sudo apt update
    sudo apt install -y python3 python3-venv python3-pip build-essential || {
      err "apt install failed. Please install python3, python3-venv manually."
      exit 1
    }
  else
    err "Automatic prereq install not supported for this OS."
    exit 1
  fi
}

main() {
  # Optionally install system packages
  if $INSTALL_PREREQS; then
    install_prereqs
  fi

  # Choose python executable
  PYTHON="$(find_python || true)"
  if [[ -z "$PYTHON" ]]; then
    err "No python executable found. Install Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+ or run with --install-prereqs."
    exit 1
  fi
  log "Using python: $PYTHON"

  if ! check_python_version "$PYTHON"; then
    err "Python at '$PYTHON' is older than ${MIN_PY_MAJOR}.${MIN_PY_MINOR}. Install a newer Python or use --install-prereqs."
    "$PYTHON" --version || true
    exit 2
  fi

  # Recreate venv if forced
  if $FORCE && [[ -d "$VENV_DIR" ]]; then
    log "--force given: removing existing venv at $VENV_DIR"
    rm -rf "$VENV_DIR"
  fi

  # Create venv if missing
  if [[ ! -d "$VENV_DIR" ]]; then
    log "Creating virtual environment at $VENV_DIR..."
    "$PYTHON" -m venv "$VENV_DIR"
  else
    log "Virtual environment already exists at $VENV_DIR"
  fi

  PIP="$VENV_DIR/bin/pip"
  PY_BIN="$VENV_DIR/bin/python"
  if [[ ! -x "$PIP" ]]; then
    err "pip not found inside venv. Attempting to bootstrap ensurepip..."
    "$PYTHON" -m ensurepip --upgrade
    "$PYTHON" -m pip install --upgrade pip
  fi

  log "Upgrading pip in venv..."
  "$PIP" install --upgrade pip setuptools wheel

  # Install requirements if the file exists
  if [[ -f "$REQUIREMENTS" ]]; then
    log "Installing Python dependencies from $REQUIREMENTS..."
    "$PIP" install -r "$REQUIREMENTS"
  else
    log "Requirements file '$REQUIREMENTS' not found; skipping pip install."
  fi

  log "Setup completed."
  cat <<EOF

Next steps:
  - Activate the venv:
      macOS / Linux:  source $VENV_DIR/bin/activate
      Or use the venv python directly: $PY_BIN

  - Run the CLI (example):
      source $VENV_DIR/bin/activate
      python -m yolo_router

EOF
}

main "$@"
