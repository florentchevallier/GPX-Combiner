# GPX-Combiner
GPX Combiner helps you do simple things with GPX files such as combining them, and help you fetch them from Strava if needed


If you don't have python, download the latest stable version from https://www.python.org/downloads/macos/

GPX combiner helps you download activities from Strava or local GPX files to combine them into a single activity.

Start with this command in Terminal:

python3 "/pathname/gpx_combiner.py"

if it doesn't work:
Install homebrew

install Tk
brew install python-tk


check latest versions:
brew update
brew upgrade tcl-tk
brew reinstall python-tk@3.13 (replace by your python version)

if issues test tk with:
python3 -c "import tkinter; tkinter._test()"



To use drag and drop: insall tkinterdnd2:

pip3 install tkinterdnd2

For Strava functions:

You need a Strava developer API: 
- go to https://www.strava.com/settings/api (with your own account)
- note client ID and Client secret to give to the app (only sotred locally)

if no activity is loading from strava, check your version of python:
python3 --version 

and then adapt the following command with your own version number:

open "/Applications/Python 3.13/Install Certificates.command"