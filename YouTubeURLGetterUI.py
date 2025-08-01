import sys
import os
import pandas as pd
import json
import keyring
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QFileDialog, QMessageBox, QTabWidget, QListWidget, QListWidgetItem,
    QProgressBar, QComboBox
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QUrl
from PyQt5.QtGui import QDesktopServices, QIcon
from PyQt5.QtWidgets import QHeaderView

# Prevent Application Default Credentials (ADC) usage to avoid Google API authentication issues
os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

def resource_path(relative_path):
    """Get absolute path to resource, works for development and PyInstaller.

    Args:
        relative_path (str): Relative path to the resource file (e.g., 'youtube_icon.png').

    Returns:
        str: Absolute path to the resource, accounting for PyInstaller’s temporary folder.
    """
    try:
        # PyInstaller stores resources in a temporary folder accessed via _MEIPASS
        base_path = sys._MEIPASS
    except AttributeError:
        # In development, use the current directory
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

class ScraperThread(QThread):
    """Thread for scraping YouTube channel playlists using the YouTube Data API v3."""
    
    # Signals for UI updates
    status_update = pyqtSignal(str)  # Emits status messages (e.g., "Processing @YouTubeCreators")
    add_row = pyqtSignal(str, str, str)  # Emits channel_handle (without @), title, url for table rows
    new_tab = pyqtSignal(str)  # Emits channel_handle (without @) for new tabs
    progress_update = pyqtSignal(int)  # Emits progress percentage for progress bar
    enable_save_button = pyqtSignal(bool)  # Enables/disables save button
    error_occurred = pyqtSignal(str)  # Emits error messages for popups

    def __init__(self, api_key, channel_handles):
        """Initialize the scraper thread with API key and channel handles.

        Args:
            api_key (str): YouTube Data API v3 key for authentication.
            channel_handles (list): List of channel handles (e.g., ['@YouTubeCreators']).
        """
        super().__init__()
        self.api_key = api_key
        self.channel_handles = channel_handles

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(HttpError)
    )
    def call_playlist_items_api(self, service, playlist_id, next_page_token):
        """Fetch playlist items from YouTube API with retry logic for network errors.

        Args:
            service: Initialized YouTube API service object (googleapiclient.discovery.Resource).
            playlist_id (str): ID of the playlist to fetch.
            next_page_token (str): Token for paginated results, or None for first page.

        Returns:
            dict: API response containing playlist items.

        Raises:
            ValueError: If the playlist is not found (HTTP 404).
            Exception: For other API errors (e.g., network issues, quota exceeded).
        """
        try:
            if next_page_token:
                request = service.playlistItems().list(
                    part='contentDetails',
                    playlistId=playlist_id,
                    maxResults=50,
                    pageToken=next_page_token
                )
            else:
                request = service.playlistItems().list(
                    part='contentDetails',
                    playlistId=playlist_id,
                    maxResults=50
                )
            return request.execute()
        except HttpError as e:
            if e.resp.status == 404:
                raise ValueError("No uploads playlist found")
            raise Exception(f"Error in playlistItems API: {e}")
        except Exception as e:
            raise Exception(f"Error in playlistItems API: {e}")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(HttpError)
    )
    def call_channels_api(self, service, channel_id):
        """Fetch channel details from YouTube API with retry logic for network errors.

        Args:
            service: Initialized YouTube API service object.
            channel_id (str): ID of the channel to fetch.

        Returns:
            dict: API response containing channel details (e.g., uploads playlist ID).

        Raises:
            Exception: For API errors (e.g., invalid channel ID, network issues).
        """
        try:
            request = service.channels().list(
                part='contentDetails',
                id=channel_id
            )
            return request.execute()
        except Exception as e:
            raise Exception(f"Error in channels API: {e}")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(HttpError)
    )
    def call_videos_api(self, service, video_ids):
        """Fetch video details from YouTube API with retry logic for network errors.

        Args:
            service: Initialized YouTube API service object.
            video_ids (list): List of video IDs to fetch.

        Returns:
            dict: API response containing video details (e.g., titles, IDs).

        Raises:
            Exception: For API errors (e.g., invalid video IDs, network issues).
        """
        try:
            request = service.videos().list(
                part='snippet',
                id=','.join(video_ids)
            )
            return request.execute()
        except Exception as e:
            raise Exception(f"Error in videos API: {e}")

    def get_channel_id(self, service, channel_handle):
        """Fetch channel ID from YouTube API using the channel handle.

        Args:
            service: Initialized YouTube API service object.
            channel_handle (str): Channel handle (e.g., '@YouTubeCreators').

        Returns:
            str: Channel ID (e.g., 'UCBR8-60-B28hp2BmDPdntcQ').

        Raises:
            ValueError: If no channel is found for the handle.
            Exception: For other API errors (e.g., network issues).
        """
        try:
            request = service.channels().list(part='id', forHandle=channel_handle)
            response = request.execute()
            channel_id = response['items'][0]['id'] if response.get('items') else None
            if not channel_id:
                raise ValueError(f"No channel found for handle {channel_handle}")
            return channel_id
        except ValueError as e:
            raise e
        except Exception as e:
            raise Exception(f"Error fetching channel ID for {channel_handle}: {e}")

    def run(self):
        """Main thread execution to scrape playlists for all provided channels."""
        try:
            # Validate inputs to ensure API key and at least one channel handle are provided
            self.status_update.emit("Starting...")
            if not self.api_key:
                raise ValueError("API key is required")
            if not self.channel_handles:
                raise ValueError("At least one channel handle is required")

            # Initialize YouTube API service with the provided API key
            service = build('youtube', 'v3', developerKey=self.api_key, credentials=None)
            total_videos = 0
            processed_channels = set()  # Track processed channels to avoid duplicates

            # Process each channel handle
            for channel_handle in self.channel_handles:
                channel_handle = channel_handle.strip()
                if not channel_handle or channel_handle in processed_channels:
                    continue

                self.status_update.emit(f"Processing {channel_handle}...")
                try:
                    # Fetch channel ID and uploads playlist
                    channel_id = self.get_channel_id(service, channel_handle)
                    channels_response = self.call_channels_api(service, channel_id)
                    if not channels_response or 'items' not in channels_response:
                        raise ValueError(f"Failed to retrieve channel data for {channel_handle}")
                    uploads_playlist_id = channels_response['items'][0]['contentDetails']['relatedPlaylists']['uploads']
                    playlist_response = self.call_playlist_items_api(service, uploads_playlist_id, None)
                    total_videos += playlist_response.get('pageInfo', {}).get('totalResults', 0)

                    # Use display handle (without @) for UI consistency with tabs and selection list
                    display_handle = channel_handle[1:] if channel_handle.startswith('@') else channel_handle
                    self.new_tab.emit(display_handle)
                    processed_channels.add(channel_handle)

                    if not playlist_response or 'items' not in playlist_response:
                        raise ValueError(f"Failed to retrieve playlist items for {channel_handle}")

                    # Process playlist pages with pagination
                    has_next_page = True
                    processed_videos = 0
                    while has_next_page:
                        self.status_update.emit(f"Processing page for {channel_handle}...")
                        video_ids = [item['contentDetails']['videoId'] for item in playlist_response['items']]
                        videos_response = self.call_videos_api(service, video_ids)
                        if not videos_response or 'items' not in videos_response:
                            raise ValueError(f"Failed to retrieve video data for {channel_handle}")

                        # Add video data to table using display handle
                        for item in videos_response['items']:
                            video_title = item['snippet']['title']
                            video_id = item['id']
                            self.add_row.emit(display_handle, video_title, f"https://www.youtube.com/watch?v={video_id}")
                            processed_videos += 1
                            if total_videos > 0:
                                self.progress_update.emit(int(processed_videos / total_videos * 100))

                        # Handle pagination
                        if 'nextPageToken' in playlist_response:
                            next_page = playlist_response['nextPageToken']
                            playlist_response = self.call_playlist_items_api(service, uploads_playlist_id, next_page)
                        else:
                            has_next_page = False
                except ValueError as e:
                    self.error_occurred.emit(f"{str(e)} for handle {channel_handle}")
                    processed_channels.add(channel_handle)
                    continue
                except Exception as e:
                    self.error_occurred.emit(f"Error processing {channel_handle}: {str(e)}")
                    processed_channels.add(channel_handle)
                    continue

            self.status_update.emit("Complete")
            self.enable_save_button.emit(True)
        except Exception as e:
            self.error_occurred.emit(str(e))

class YouTubeScraperWindow(QMainWindow):
    """Main window for the YouTube Playlist Scraper GUI."""

    def __init__(self):
        """Initialize the main window with UI components and dark theme."""
        super().__init__()
        self.setWindowTitle("YouTube Playlist Scraper")
        self.setGeometry(100, 100, 1000, 600)

        # Set YouTube-like window icon, handling PyInstaller resource path
        try:
            self.setWindowIcon(QIcon(resource_path("youtube_icon.png")))
        except Exception as e:
            print(f"Warning: Could not load window icon: {e}")

        # Apply dark theme stylesheet
        # Note: Use /* */ for comments, as # causes parsing errors (e.g., "Could not parse stylesheet")
        self.setStyleSheet("""
            QMainWindow {
                background-color: #2e2e2e; /* Dark gray background for main window */
            }
            QLabel {
                font-size: 14px;
                color: #e0e0e0; /* Light gray text */
            }
            QLabel#statusLabel {
                font-size: 16px; /* Larger font for status label visibility */
                color: #e0e0e0;
                padding: 8px; /* Padding for prominence */
                background-color: #424242; /* Darker gray background to stand out */
                border-radius: 5px;
            }
            QLineEdit, QTextEdit, QListWidget, QComboBox {
                border: 1px solid #424242;
                border-radius: 5px;
                padding: 5px;
                background-color: #616161; /* Medium gray background */
                color: #e0e0e0;
            }
            QTextEdit:focus, QLineEdit:focus, QListWidget:focus, QComboBox:focus {
                border: 1px solid #26a69a; /* Teal border on focus */
            }
            QPushButton {
                background-color: #26a69a; /* Teal button background */
                color: #ffffff;
                border: none;
                border-radius: 5px;
                padding: 8px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #4dd0e1; /* Lighter teal on hover */
            }
            QPushButton:disabled {
                background-color: #78909c; /* Gray for disabled buttons */
            }
            QPushButton#apiKeyInfoButton {
                background-color: #0288d1; /* Blue for "What's this?" button */
                font-size: 12px;
                padding: 4px;
                min-width: 80px;
                min-height: 24px;
            }
            QPushButton#apiKeyInfoButton:hover {
                background-color: #03a9f4; /* Lighter blue on hover */
            }
            QTabWidget::pane {
                border: 1px solid #424242;
                border-radius: 5px;
                background-color: #424242; /* Dark gray tab pane */
            }
            QTabBar::tab {
                background: #616161;
                color: #e0e0e0;
                border: none;
                padding: 8px 15px;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background: #26a69a; /* Teal for selected tab */
                color: #ffffff;
            }
            QTableWidget {
                border: 1px solid #424242;
                border-radius: 5px;
                background-color: #424242; /* Dark gray table background */
                color: #e0e0e0;
            }
            QTableWidget::item:hover {
                background-color: #4dd0e1; /* Teal on hover */
            }
            QHeaderView::section {
                background-color: #616161; /* Medium gray for table headers */
                color: #e0e0e0;
                padding: 4px;
                border: 1px solid #424242;
            }
            QTableWidget QTableCornerButton::section {
                background-color: #616161; /* Medium gray for table corner button to match headers */
                border: 1px solid #424242;
            }
            QProgressBar {
                border: 1px solid #424242;
                border-radius: 5px;
                text-align: center;
                background-color: #616161;
                color: #e0e0e0;
            }
            QProgressBar::chunk {
                background-color: #26a69a; /* Teal progress bar fill */
                border-radius: 3px;
            }
            QMessageBox {
                background-color: #2e2e2e; /* Dark gray popup background */
                color: #e0e0e0;
            }
            QMessageBox QLabel {
                color: #e0e0e0;
            }
            QMessageBox QPushButton {
                background-color: #26a69a;
                color: #ffffff;
                border: none;
                border-radius: 5px;
                padding: 6px;
                min-width: 120px; /* Minimum width for popup buttons, e.g., 'Ok' in "What's this?" */
            }
            QMessageBox QPushButton:hover {
                background-color: #4dd0e1;
            }
            QMessageBox QPushButton:disabled {
                background-color: #78909c;
            }
        """)

        # Main widget and layout
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QHBoxLayout(self.central_widget)

        # Sidebar Setup
        self.sidebar_widget = QWidget()
        self.sidebar_widget.setFixedWidth(320)  # Fixed 320px width for sidebar
        self.sidebar_layout = QVBoxLayout(self.sidebar_widget)
        self.sidebar_layout.setContentsMargins(10, 10, 10, 10)
        self.sidebar_layout.setSpacing(12)

        # API Key Input with Info Button
        self.api_key_layout = QHBoxLayout()
        self.api_key_label = QLabel("YouTube API Key:")
        self.api_key_label.setToolTip("Enter your YouTube Data API v3 key")
        self.api_key_entry = QLineEdit()
        self.api_key_entry.setPlaceholderText("Enter your API key")
        self.api_key_entry.setToolTip("Obtain from Google Cloud Console")
        self.api_key_entry.setEchoMode(QLineEdit.Password)  # Mask API key input
        self.api_key_info_button = QPushButton("What's this?")
        self.api_key_info_button.setObjectName("apiKeyInfoButton")
        self.api_key_info_button.setFixedWidth(100)
        self.api_key_info_button.setToolTip("Learn about YouTube API keys")
        self.api_key_info_button.clicked.connect(self.show_api_key_info)
        self.api_key_layout.addWidget(self.api_key_entry)
        self.api_key_layout.addWidget(self.api_key_info_button)
        self.sidebar_layout.addWidget(self.api_key_label)
        self.sidebar_layout.addLayout(self.api_key_layout)

        # Load saved API key from keyring (secure storage)
        saved_api_key = keyring.get_password("YouTubeScraper", "api_key")
        if saved_api_key:
            self.api_key_entry.setText(saved_api_key)

        # Channel Handles Input
        self.channel_label = QLabel("Channel Handles (one per line):")
        self.channel_label.setToolTip("Enter channel handles, e.g., @YouTubeCreators")
        self.channel_text = QTextEdit()
        self.channel_text.setPlaceholderText("@YouTubeCreators\n@Markiplier\n@Pewdiepie")
        self.channel_text.setToolTip("Enter one channel handle per line")
        self.sidebar_layout.addWidget(self.channel_label)
        self.sidebar_layout.addWidget(self.channel_text)

        # Channel Selection List for Export
        self.channel_list_label = QLabel("Select Channels to Save:")
        self.channel_list_label.setToolTip("Check channels to include in export")
        self.channel_list = QListWidget()
        self.channel_list.setFixedHeight(120)
        self.channel_list.setToolTip("Select channels to export as CSV or JSON")
        self.sidebar_layout.addWidget(self.channel_list_label)
        self.sidebar_layout.addWidget(self.channel_list)

        # Export Format Selection
        self.export_format_label = QLabel("Export Format:")
        self.export_format_label.setToolTip("Choose output file format")
        self.export_format_combo = QComboBox()
        self.export_format_combo.addItems(["CSV", "JSON"])
        self.export_format_combo.setToolTip("Export as CSV or JSON")
        self.sidebar_layout.addWidget(self.export_format_label)
        self.sidebar_layout.addWidget(self.export_format_combo)

        # Run Button
        self.run_button = QPushButton("Run")
        self.run_button.clicked.connect(self.start_scraping)
        self.run_button.setToolTip("Start scraping videos from entered channels")
        self.sidebar_layout.addWidget(self.run_button)

        # Clear Inputs Button
        self.clear_button = QPushButton("Clear Inputs")
        self.clear_button.clicked.connect(self.clear_inputs)
        self.clear_button.setToolTip("Reset channel handle input")
        self.sidebar_layout.addWidget(self.clear_button)

        # Save Button
        self.save_button = QPushButton("Save Selected to CSV/JSON")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_to_file)
        self.save_button.setToolTip("Save selected channels to files in chosen format")
        self.sidebar_layout.addWidget(self.save_button)

        # Spacer to push controls to top
        self.sidebar_layout.addStretch()

        # Main Area Setup
        self.results_widget = QWidget()
        self.results_layout = QVBoxLayout(self.results_widget)
        self.results_layout.setContentsMargins(10, 10, 10, 10)
        self.results_layout.setSpacing(10)

        # Status Label (moved above progress bar for visibility and to avoid truncation)
        self.status_label = QLabel("Status: Idle")
        self.status_label.setObjectName("statusLabel")  # For stylesheet targeting
        self.status_label.setToolTip("Shows the current scraping status")
        self.results_layout.addWidget(self.status_label)

        # Tab Widget for Results
        self.tab_widget = QTabWidget()
        self.tables = {}  # Dictionary to store tables for each channel
        self.results_layout.addWidget(self.tab_widget)

        # Progress Bar (hidden by default)
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        self.results_layout.addWidget(self.progress_bar)

        # Combine sidebar and results in main layout
        self.main_layout.addWidget(self.sidebar_widget)
        self.main_layout.addWidget(self.results_widget, stretch=1)

        # Initialize results storage for video data
        self.url_list = []

    def show_api_key_info(self):
        """Display a popup explaining the YouTube API key and how to obtain one."""
        msg_box = QMessageBox()
        msg_box.setWindowTitle("YouTube API Key Info")
        msg_box.setText(
            "A YouTube Data API v3 key is a unique string that authenticates requests to fetch YouTube video data.\n\n"
            "To get one:\n"
            "1. Go to Google Cloud Console (https://console.cloud.google.com).\n"
            "2. Sign in with your Google account.\n"
            "3. Create a new project or select an existing one.\n"
            "4. Enable the 'YouTube Data API v3' in the APIs & Services section.\n"
            "5. Create an API key in the Credentials section.\n"
            "6. Copy the key and paste it into the input field."
        )
        try:
            msg_box.setWindowIcon(QIcon(resource_path("youtube_icon.png")))
        except Exception as e:
            print(f"Warning: Could not load popup icon: {e}")
        msg_box.setStandardButtons(QMessageBox.Ok)  # Only Ok button, close button enabled by default
        msg_box.setStyleSheet(self.styleSheet())  # Apply dark theme with 120px buttons
        # Add button to open Google Cloud Console without closing dialog
        console_button = QPushButton("Open Google Cloud Console")
        console_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://console.cloud.google.com")))
        msg_box.addButton(console_button, QMessageBox.ActionRole)
        msg_box.exec_()

    def create_new_tab(self, channel_handle):
        """Create a new tab with a table for a channel's videos.

        Args:
            channel_handle (str): Channel handle for the tab (without @, e.g., 'YouTubeCreators').
        """
        # Set up table with two columns
        table = QTableWidget()
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["Video Title", "Video URL"])
        table.setColumnWidth(0, 400)  # Fixed width for title column
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)  # Auto-expand URL column
        table.setEditTriggers(QTableWidget.NoEditTriggers)  # Disable editing
        table.itemDoubleClicked.connect(self.table_clicked)
        self.tables[channel_handle] = table
        self.tab_widget.addTab(table, channel_handle)

        # Add channel to selection list with default checked state
        item = QListWidgetItem(channel_handle)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked)
        self.channel_list.addItem(item)

    def table_clicked(self, item):
        """Handle double-click on table items to open URLs.

        Args:
            item: QTableWidgetItem that was double-clicked.
        """
        if item.column() == 1:  # URL column
            QDesktopServices.openUrl(QUrl(item.text()))

    def clear_inputs(self):
        """Clear channel handle input field, preserving API key per user preference."""
        self.channel_text.clear()

    def start_scraping(self):
        """Start the scraping process in a separate thread."""
        api_key = self.api_key_entry.text().strip()
        if api_key:
            try:
                keyring.set_password("YouTubeScraper", "api_key", api_key)
            except keyring.errors.KeyringError as e:
                print(f"Warning: Could not save API key to keyring: {e}")  # May occur on unsupported platforms

        # Reset UI state for new scrape
        self.tab_widget.clear()
        self.tables.clear()
        self.channel_list.clear()
        self.url_list = []
        self.run_button.setEnabled(False)
        self.clear_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)

        # Get channel handles from text input, removing empty lines
        channel_handles = [channel_input.strip() for channel_input in self.channel_text.toPlainText().splitlines() if channel_input.strip()]

        # Start scraper thread
        self.scraper_thread = ScraperThread(api_key, channel_handles)
        self.scraper_thread.status_update.connect(self.update_status)
        self.scraper_thread.add_row.connect(self.add_table_row)
        self.scraper_thread.new_tab.connect(self.create_new_tab)
        self.scraper_thread.progress_update.connect(self.progress_bar.setValue)
        self.scraper_thread.enable_save_button.connect(self.save_button.setEnabled)
        self.scraper_thread.error_occurred.connect(self.show_error)
        self.scraper_thread.finished.connect(self.scraping_finished)
        self.scraper_thread.start()

    def update_status(self, status):
        """Update the status label with the current scraping status.

        Args:
            status (str): Status message to display (e.g., 'Processing @YouTubeCreators').
        """
        self.status_label.setText(f"Status: {status}")

    def add_table_row(self, channel_handle, title, url):
        """Add a row to the table for a channel's video.

        Args:
            channel_handle (str): Channel handle for the video (without @, e.g., 'YouTubeCreators').
            title (str): Video title.
            url (str): Video URL.
        """
        if channel_handle not in self.tables:
            self.create_new_tab(channel_handle)
        table = self.tables[channel_handle]
        table.setAccessibleName(channel_handle)  # For identifying table in table_clicked
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(title))
        table.setItem(row, 1, QTableWidgetItem(url))
        self.url_list.append([channel_handle, title, url])
        # Resize URL column to fit content
        table.resizeColumnToContents(1)

    def show_error(self, error_message):
        """Display an error message in a dark-themed popup.

        Args:
            error_message (str): Error message to display (e.g., 'No channel found for handle @Invalid').
        """
        msg_box = QMessageBox()
        msg_box.setWindowTitle("Error")
        msg_box.setText(error_message)
        try:
            msg_box.setWindowIcon(QIcon(resource_path("youtube_icon.png")))
        except Exception as e:
            print(f"Warning: Could not load popup icon: {e}")
        msg_box.setStyleSheet(self.styleSheet())  # Apply dark theme
        msg_box.exec_()
        self.status_label.setText("Status: Error")
        self.progress_bar.setVisible(False)
        self.run_button.setEnabled(True)
        self.clear_button.setEnabled(True)

    def scraping_finished(self):
        """Handle completion of the scraping thread."""
        self.run_button.setEnabled(True)
        self.clear_button.setEnabled(True)
        self.progress_bar.setVisible(False)

    def save_to_file(self):
        """Save selected channels' data to CSV or JSON files.

        Note: Uses display handles (without @) in url_list to match QListWidget entries.
        """
        if not self.url_list:
            msg_box = QMessageBox()
            msg_box.setWindowTitle("Warning")
            msg_box.setText("No data to save")
            try:
                msg_box.setWindowIcon(QIcon(resource_path("youtube_icon.png")))
            except Exception as e:
                print(f"Warning: Could not load popup icon: {e}")
            msg_box.setStyleSheet(self.styleSheet())
            msg_box.exec_()
            return

        # Get selected channels from checklist
        selected_channels = []
        for i in range(self.channel_list.count()):
            item = self.channel_list.item(i)
            if item.checkState() == Qt.Checked:
                selected_channels.append(item.text())

        if not selected_channels:
            msg_box = QMessageBox()
            msg_box.setWindowTitle("Warning")
            msg_box.setText("No channels selected")
            try:
                msg_box.setWindowIcon(QIcon(resource_path("youtube_icon.png")))
            except Exception as e:
                print(f"Warning: Could not load popup icon: {e}")
            msg_box.setStyleSheet(self.styleSheet())
            msg_box.exec_()
            return

        # Prompt for output directory
        output_dir = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if not output_dir:
            return

        # Organize data by channel
        export_format = self.export_format_combo.currentText().lower()
        channel_data = {}
        for channel_handle, title, url in self.url_list:
            if channel_handle in selected_channels:
                if channel_handle not in channel_data:
                    channel_data[channel_handle] = []
                channel_data[channel_handle].append([title, url])

        # Save files for each channel
        for channel_handle, data in channel_data.items():
            # Sanitize filename by keeping alphanumeric and safe characters
            safe_handle = "".join(char for char in channel_handle if char.isalnum() or char in ('-', '_')).rstrip()
            file_path = os.path.join(output_dir, f"{safe_handle}_output.{export_format}")
            try:
                if export_format == 'csv':
                    df = pd.DataFrame(data, columns=['Title', 'Url'])
                    df.to_csv(file_path, index=True, encoding='utf-8')
                else:  # JSON
                    with open(file_path, 'w', encoding='utf-8') as f:
                        json.dump([{'Title': title, 'Url': url} for title, url in data], f, indent=2)
                self.status_label.setText(f"Status: Saved {file_path}")
            except Exception as e:
                msg_box = QMessageBox()
                msg_box.setWindowTitle("Error")
                msg_box.setText(f"Failed to save {file_path}: {str(e)}")
                try:
                    msg_box.setWindowIcon(QIcon(resource_path("youtube_icon.png")))
                except Exception as e:
                    print(f"Warning: Could not load popup icon: {e}")
                msg_box.setStyleSheet(self.styleSheet())
                msg_box.exec_()
                continue

        # Show success message only if at least one file was saved
        if channel_data:
            msg_box = QMessageBox()
            msg_box.setWindowTitle("Success")
            msg_box.setText(f"Selected channels saved to {export_format.upper()} files in {output_dir}")
            try:
                msg_box.setWindowIcon(QIcon(resource_path("youtube_icon.png")))
            except Exception as e:
                print(f"Warning: Could not load popup icon: {e}")
            msg_box.setStyleSheet(self.styleSheet())
            msg_box.exec_()

if __name__ == "__main__":
    """Entry point to launch the application."""
    app = QApplication(sys.argv)
    window = YouTubeScraperWindow()
    window.show()
    sys.exit(app.exec_())