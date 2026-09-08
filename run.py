"""
HDTodayz Video Downloader - Startup Launcher
"""

import sys
import asyncio
from pathlib import Path

# Fix Windows IOCP WinError 64 connection reset crashes
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn
import webbrowser

# Ensure root is in sys.path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

def main():
    port = int(os.environ.get("PORT", 7860))
    host = os.environ.get("HOST", "0.0.0.0" if os.environ.get("PORT") else "0.0.0.0")

    print("=" * 65)
    print("       HDTodayz Automated Video Downloader & Stream Engine       ")
    print("=" * 65)
    print(f"  • Web UI:        http://{host}:{port}")
    print(f"  • Downloads Dir: {BASE_DIR / 'downloads'}")
    print(f"  • Target Site:   https://hdtodayz.org/home")
    print("=" * 65)
    print("Starting server... Press Ctrl+C to stop.\n")

    uvicorn.run("app.main:app", host=host, port=port, reload=False, log_level="info")

if __name__ == "__main__":
    main()
