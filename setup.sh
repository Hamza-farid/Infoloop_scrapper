#!/bin/bash
# NOTE: Streamlit Community Cloud does NOT run this file. It only processes
# packages.txt (apt) and requirements.txt (pip).
#
# On Streamlit Cloud the browser comes from packages.txt -> `chromium`, and
# worker.py picks it up automatically via resolve_chromium(). If that is ever
# missing, worker.py falls back to running `playwright install chromium` itself.
#
# This script is here for OTHER hosts (a VPS, Docker, Railway, Fly.io) where you
# do control the build step.
set -e
playwright install chromium
