#!/usr/bin/env python
"""
GridWise API startup script.
Usage: python run.py
"""
import os
import sys
import subprocess

def main():
    # Check for API key
    if not os.environ.get("GEMINI_API_KEY"):
        print("WARNING: GEMINI_API_KEY is not set.")
        print("The service will start but /optimize-energy will fail until you set it.")
        print("Set it with: $env:GEMINI_API_KEY = 'your_key_here'")
        print()

    port = os.environ.get("PORT", "8000")
    
    cmd = [
        sys.executable, "-m", "uvicorn",
        "app.main:app",
        "--host", "0.0.0.0",
        "--port", port,
        "--log-level", "info",
    ]
    
    print(f"Starting GridWise API on port {port}...")
    print(f"Health: http://localhost:{port}/health")
    print(f"API:    http://localhost:{port}/optimize-energy")
    print()
    
    subprocess.run(cmd)


if __name__ == "__main__":
    main()
