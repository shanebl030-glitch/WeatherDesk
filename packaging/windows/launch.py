"""Entry point for the Windows portable build (PyInstaller). Created By Shane BlackbuRn"""
import sys

from rbsb_weather.app import main

if __name__ == "__main__":
    sys.exit(main())
