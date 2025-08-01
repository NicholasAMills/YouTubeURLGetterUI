# YouTubeURLGetterUI

An improved version of YouTubeURLGetter. No longer requires manually searching for channel ID's and implements a full UI
Build command: pyinstaller --onefile --add-data "youtube_icon.png;." --hidden-import=googleapiclient --hidden-import=keyring.backends --windowed --name YouTubeScraper YouTubeURLGetterUI.py
