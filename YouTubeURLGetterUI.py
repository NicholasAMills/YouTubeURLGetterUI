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

# Prevent Application Default Credentials (ADC) usage
os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

class ScraperThread(QThread):
    """Thread for scraping YouTube channel playlists using the YouTube Data API v3."""
    
    status_update = pyqtSignal(str)
    add_row = pyqtSignal(str, str, str)
    new_tab = pyqtSignal(str)
    progress_update = pyqtSignal(int)
    enable_save_button = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)

    def __init__(self, api_key, channel_handles):
        super().__init__()
        self.api_key = api_key
        self.channel_handles = channel_handles

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type(HttpError)
    )
    def call_playlist_items_api(self, service, playlist_id, next_page_token):
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
        try:
            request = service.videos().list(
                part='snippet',
                id=','.join(video_ids)
            )
            return request.execute()
        except Exception as e:
            raise Exception(f"Error in videos API: {e}")

    def get_channel_id(self, service, channel_handle):
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
        try:
            self.status_update.emit("Starting...")
            if not self.api_key:
                raise ValueError("API key is required")
            if not self.channel_handles:
                raise ValueError("At least one channel handle is required")

            service = build('youtube', 'v3', developerKey=self.api_key, credentials=None)
            total_videos = 0
            processed_channels = set()

            for channel_handle in self.channel_handles:
                channel_handle = channel_handle.strip()
                if not channel_handle or channel_handle in processed_channels:
                    continue

                self.status_update.emit(f"Processing {channel_handle}...")
                try:
                    channel_id = self.get_channel_id(service, channel_handle)
                    channels_response = self.call_channels_api(service, channel_id)
                    if not channels_response or 'items' not in channels_response:
                        raise ValueError(f"Failed to retrieve channel data for {channel_handle}")
                    uploads_playlist_id = channels_response['items'][0]['contentDetails']['relatedPlaylists']['uploads']
                    playlist_response = self.call_playlist_items_api(service, uploads_playlist_id, None)
                    total_videos += playlist_response.get('pageInfo', {}).get('totalResults', 0)

                    self.new_tab.emit(channel_handle)
                    processed_channels.add(channel_handle)

                    if not playlist_response or 'items' not in playlist_response:
                        raise ValueError(f"Failed to retrieve playlist items for {channel_handle}")

                    has_next_page = True
                    processed_videos = 0
                    while has_next_page:
                        self.status_update.emit(f"Processing page for {channel_handle}...")
                        video_ids = [i['contentDetails']['videoId'] for i in playlist_response['items']]
                        videos_response = self.call_videos_api(service, video_ids)
                        if not videos_response or 'items' not in videos_response:
                            raise ValueError(f"Failed to retrieve video data for {channel_handle}")

                        for item in videos_response['items']:
                            video_title = item['snippet']['title']
                            video_id = item['id']
                            self.add_row.emit(channel_handle, video_title, f"https://www.youtube.com/watch?v={video_id}")
                            processed_videos += 1
                            if total_videos > 0:
                                self.progress_update.emit(int(processed_videos / total_videos * 100))

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
        super().__init__()
        self.setWindowTitle("YouTube Playlist Scraper")
        self.setGeometry(100, 100, 1000, 600)

        try:
            self.setWindowIcon(QIcon("youtube_icon.png"))
        except Exception as e:
            print(f"Warning: Could not load window icon: {e}")

        self.setStyleSheet("""
            QMainWindow { background-color: #2e2e2e; }
            QLabel { font-size: 14px; color: #e0e0e0; }
            QLineEdit, QTextEdit, QListWidget, QComboBox {
                border: 1px solid #424242;
                border-radius: 5px;
                padding: 5px;
                background-color: #616161;
                color: #e0e0e0;
            }
            QTextEdit:focus, QLineEdit:focus, QListWidget:focus, QComboBox:focus {
                border: 1px solid #26a69a;
            }
            QPushButton {
                background-color: #26a69a;
                color: #ffffff;
                border: none;
                border-radius: 5px;
                padding: 8px;
                font-size: 14px;
            }
            QPushButton:hover {
                background-color: #4dd0e1;
            }
            QPushButton:disabled {
                background-color: #78909c;
            }
            QPushButton#apiKeyInfoButton {
                background-color: #0288d1;
                font-size: 12px;
                padding: 4px;
                min-width: 80px;
                min-height: 24px;
            }
            QPushButton#apiKeyInfoButton:hover {
                background-color: #03a9f4;
            }
            QTabWidget::pane {
                border: 1px solid #424242;
                border-radius: 5px;
                background-color: #424242;
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
                background: #26a69a;
                color: #ffffff;
            }
            QTableWidget {
                border: 1px solid #424242;
                border-radius: 5px;
                background-color: #424242;
                color: #e0e0e0;
            }
            QTableWidget::item:hover {
                background-color: #4dd0e1;
            }
            QHeaderView::section {
                background-color: #616161;
                color: #e0e0e0;
                padding: 4px;
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
                background-color: #26a69a;
                border-radius: 3px;
            }
            QMessageBox {
                background-color: #2e2e2e;
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
            }
            QMessageBox QPushButton:hover {
                background-color: #4dd0e1;
            }
            QMessageBox QPushButton:disabled {
                background-color: #78909c;
            }
        """)

        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QHBoxLayout(self.central_widget)

        self.sidebar_widget = QWidget()
        self.sidebar_widget.setFixedWidth(320)
        self.sidebar_layout = QVBoxLayout(self.sidebar_widget)
        self.sidebar_layout.setContentsMargins(10, 10, 10, 10)
        self.sidebar_layout.setSpacing(12)

        self.api_key_layout = QHBoxLayout()
        self.api_key_label = QLabel("YouTube API Key:")
        self.api_key_label.setToolTip("Enter your YouTube Data API v3 key")
        self.api_key_entry = QLineEdit()
        self.api_key_entry.setPlaceholderText("Enter your API key")
        self.api_key_entry.setToolTip("Obtain from Google Cloud Console")
        self.api_key_entry.setEchoMode(QLineEdit.Password)  # Hide API key input
        self.api_key_info_button = QPushButton("What's this?")
        self.api_key_info_button.setObjectName("apiKeyInfoButton")
        self.api_key_info_button.setFixedWidth(100)
        self.api_key_info_button.setToolTip("Learn about YouTube API keys")
        self.api_key_info_button.clicked.connect(self.show_api_key_info)
        self.api_key_layout.addWidget(self.api_key_entry)
        self.api_key_layout.addWidget(self.api_key_info_button)
        self.sidebar_layout.addWidget(self.api_key_label)
        self.sidebar_layout.addLayout(self.api_key_layout)

        # Load saved API key
        saved_api_key = keyring.get_password("YouTubeScraper", "api_key")
        if saved_api_key:
            self.api_key_entry.setText(saved_api_key)

        self.channel_label = QLabel("Channel Handles (one per line):")
        self.channel_label.setToolTip("Enter channel handles, e.g., @YouTubeCreators")
        self.channel_text = QTextEdit()
        self.channel_text.setPlaceholderText("e.g., @YouTubeCreators\n@MrBeast")
        self.channel_text.setToolTip("Enter one channel handle per line")
        self.sidebar_layout.addWidget(self.channel_label)
        self.sidebar_layout.addWidget(self.channel_text)

        self.channel_list_label = QLabel("Select Channels to Save:")
        self.channel_list_label.setToolTip("Check channels to include in export")
        self.channel_list = QListWidget()
        self.channel_list.setFixedHeight(120)
        self.channel_list.setToolTip("Select channels to export as CSV or JSON")
        self.sidebar_layout.addWidget(self.channel_list_label)
        self.sidebar_layout.addWidget(self.channel_list)

        self.export_format_label = QLabel("Export Format:")
        self.export_format_label.setToolTip("Choose output file format")
        self.export_format_combo = QComboBox()
        self.export_format_combo.addItems(["CSV", "JSON"])
        self.export_format_combo.setToolTip("Export as CSV or JSON")
        self.sidebar_layout.addWidget(self.export_format_label)
        self.sidebar_layout.addWidget(self.export_format_combo)

        self.run_button = QPushButton("Run")
        self.run_button.clicked.connect(self.start_scraping)
        self.run_button.setToolTip("Start scraping videos from entered channels")
        self.sidebar_layout.addWidget(self.run_button)

        self.clear_button = QPushButton("Clear Inputs")
        self.clear_button.clicked.connect(self.clear_inputs)
        self.clear_button.setToolTip("Reset API key and channel inputs")
        self.sidebar_layout.addWidget(self.clear_button)

        self.save_button = QPushButton("Save Selected to CSV/JSON")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_to_file)
        self.save_button.setToolTip("Save selected channels to files in chosen format")
        self.sidebar_layout.addWidget(self.save_button)

        self.sidebar_layout.addStretch()

        self.results_widget = QWidget()
        self.results_layout = QVBoxLayout(self.results_widget)
        self.results_layout.setContentsMargins(10, 10, 10, 10)
        self.results_layout.setSpacing(10)

        self.status_label = QLabel("Status: Idle")
        self.results_layout.addWidget(self.status_label)

        self.tab_widget = QTabWidget()
        self.tables = {}
        self.results_layout.addWidget(self.tab_widget)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        self.results_layout.addWidget(self.progress_bar)

        self.main_layout.addWidget(self.sidebar_widget)
        self.main_layout.addWidget(self.results_widget, stretch=1)

        self.url_list = []

    def show_api_key_info(self):
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
            msg_box.setWindowIcon(QIcon("youtube_icon.png"))
        except Exception as e:
            print(f"Warning: Could not load popup icon: {e}")
        msg_box.setStandardButtons(QMessageBox.Ok)
        msg_box.setStyleSheet(self.styleSheet())
        console_button = QPushButton("Open Google Cloud Console")
        console_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://console.cloud.google.com")))
        msg_box.addButton(console_button, QMessageBox.ActionRole)
        msg_box.exec_()

    def create_new_tab(self, channel_handle):
        table = QTableWidget()
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["Video Title", "Video URL"])
        table.setColumnWidth(0, 400)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.itemDoubleClicked.connect(self.table_clicked)
        self.tables[channel_handle] = table
        self.tab_widget.addTab(table, channel_handle)

        item = QListWidgetItem(channel_handle)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked)
        self.channel_list.addItem(item)

    def table_clicked(self, item):
        if item.column() == 1:
            QDesktopServices.openUrl(QUrl(item.text()))

    def clear_inputs(self):
        self.api_key_entry.clear()
        self.channel_text.clear()
        # Optionally clear the saved API key
        keyring.delete_password("YouTubeScraper", "api_key")

    def start_scraping(self):
        api_key = self.api_key_entry.text().strip()
        if api_key:
            keyring.set_password("YouTubeScraper", "api_key", api_key)

        self.tab_widget.clear()
        self.tables.clear()
        self.channel_list.clear()
        self.url_list = []
        self.run_button.setEnabled(False)
        self.clear_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)

        channel_handles = [h.strip() for h in self.channel_text.toPlainText().splitlines() if h.strip()]

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
        self.status_label.setText(f"Status: {status}")

    def add_table_row(self, channel_handle, title, url):
        if channel_handle not in self.tables:
            self.create_new_tab(channel_handle)
        table = self.tables[channel_handle]
        table.setAccessibleName(channel_handle)
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(title))
        table.setItem(row, 1, QTableWidgetItem(url))
        self.url_list.append([channel_handle, title, url])
        table.resizeColumnToContents(1)

    def show_error(self, error_message):
        msg_box = QMessageBox()
        msg_box.setWindowTitle("Error")
        msg_box.setText(error_message)
        try:
            msg_box.setWindowIcon(QIcon("youtube_icon.png"))
        except Exception as e:
            print(f"Warning: Could not load popup icon: {e}")
        msg_box.setStyleSheet(self.styleSheet())
        msg_box.exec_()
        self.status_label.setText("Status: Error")
        self.progress_bar.setVisible(False)
        self.run_button.setEnabled(True)
        self.clear_button.setEnabled(True)

    def scraping_finished(self):
        self.run_button.setEnabled(True)
        self.clear_button.setEnabled(True)
        self.progress_bar.setVisible(False)

    def save_to_file(self):
        if not self.url_list:
            msg_box = QMessageBox()
            msg_box.setWindowTitle("Warning")
            msg_box.setText("No data to save")
            try:
                msg_box.setWindowIcon(QIcon("youtube_icon.png"))
            except Exception as e:
                print(f"Warning: Could not load popup icon: {e}")
            msg_box.setStyleSheet(self.styleSheet())
            msg_box.exec_()
            return

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
                msg_box.setWindowIcon(QIcon("youtube_icon.png"))
            except Exception as e:
                print(f"Warning: Could not load popup icon: {e}")
            msg_box.setStyleSheet(self.styleSheet())
            msg_box.exec_()
            return

        output_dir = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if not output_dir:
            return

        export_format = self.export_format_combo.currentText().lower()
        channel_data = {}
        for channel_handle, title, url in self.url_list:
            if channel_handle in selected_channels:
                if channel_handle not in channel_data:
                    channel_data[channel_handle] = []
                channel_data[channel_handle].append([title, url])

        for channel_handle, data in channel_data.items():
            safe_handle = "".join(c for c in channel_handle if c.isalnum() or c in ('-', '_')).rstrip()
            file_path = os.path.join(output_dir, f"{safe_handle}_output.{export_format}")
            if export_format == 'csv':
                df = pd.DataFrame(data, columns=['Title', 'Url'])
                df.to_csv(file_path, index=True)
            else:
                with open(file_path, 'w') as f:
                    json.dump([{'Title': title, 'Url': url} for title, url in data], f, indent=2)
            self.status_label.setText(f"Status: Saved {file_path}")

        msg_box = QMessageBox()
        msg_box.setWindowTitle("Success")
        msg_box.setText(f"Selected channels saved to {export_format.upper()} files in {output_dir}")
        try:
            msg_box.setWindowIcon(QIcon("youtube_icon.png"))
        except Exception as e:
            print(f"Warning: Could not load popup icon: {e}")
        msg_box.setStyleSheet(self.styleSheet())
        msg_box.exec_()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = YouTubeScraperWindow()
    window.show()
    sys.exit(app.exec_())