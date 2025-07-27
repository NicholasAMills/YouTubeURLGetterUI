import sys
import os
import pandas as pd
from googleapiclient.discovery import build
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QFileDialog, QMessageBox, QTabWidget, QListWidget, QListWidgetItem
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal

# Ensure ADC is not used
os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

class ScraperThread(QThread):
    # Signals for UI updates
    status_update = pyqtSignal(str)
    add_row = pyqtSignal(str, str, str)  # channel_handle, title, url
    new_tab = pyqtSignal(str)  # channel_handle for new tab
    enable_save_button = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)

    def __init__(self, api_key, channel_handles):
        super().__init__()
        self.api_key = api_key
        self.channel_handles = channel_handles

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
        except Exception as e:
            raise Exception(f"Error in playlistItems API: {e}")

    def call_channels_api(self, service, channel_id):
        try:
            request = service.channels().list(
                part='contentDetails',
                id=channel_id
            )
            return request.execute()
        except Exception as e:
            raise Exception(f"Error in channels API: {e}")

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
            self.url_list = []

            for channel_handle in self.channel_handles:
                channel_handle = channel_handle.strip()
                if not channel_handle:
                    continue
                self.status_update.emit(f"Processing {channel_handle}...")
                self.new_tab.emit(channel_handle)  # Signal to create a new tab

                # Get channel ID
                channel_id = self.get_channel_id(service, channel_handle)
                channels_response = self.call_channels_api(service, channel_id)
                if not channels_response or 'items' not in channels_response:
                    raise ValueError(f"Failed to retrieve channel data for {channel_handle}")
                uploads_playlist_id = channels_response['items'][0]['contentDetails']['relatedPlaylists']['uploads']

                # Fetch playlist items
                playlist_items_response = self.call_playlist_items_api(service, uploads_playlist_id, None)
                if not playlist_items_response or 'items' not in playlist_items_response:
                    raise ValueError(f"Failed to retrieve playlist items for {channel_handle}")

                has_next_page = True
                while has_next_page:
                    self.status_update.emit(f"Processing page for {channel_handle}...")
                    video_ids = [i['contentDetails']['videoId'] for i in playlist_items_response['items']]
                    videos_response = self.call_videos_api(service, video_ids)
                    if not videos_response or 'items' not in videos_response:
                        raise ValueError(f"Failed to retrieve video data for {channel_handle}")

                    for item in videos_response['items']:
                        video_title = item['snippet']['title']
                        video_id = item['id']
                        self.url_list.append([channel_handle, video_title, f"https://www.youtube.com/watch?v={video_id}"])
                        self.add_row.emit(channel_handle, video_title, f"https://www.youtube.com/watch?v={video_id}")

                    if 'nextPageToken' in playlist_items_response:
                        next_page = playlist_items_response['nextPageToken']
                        playlist_items_response = self.call_playlist_items_api(service, uploads_playlist_id, next_page)
                    else:
                        has_next_page = False

            self.status_update.emit("Complete")
            self.enable_save_button.emit(True)
        except Exception as e:
            self.error_occurred.emit(str(e))

class YouTubeScraperWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("YouTube Playlist Scraper")
        self.setGeometry(100, 100, 900, 600)

        # Main widget and layout
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.layout = QVBoxLayout(self.central_widget)

        # API Key Input
        self.api_key_layout = QHBoxLayout()
        self.api_key_label = QLabel("YouTube API Key:")
        self.api_key_entry = QLineEdit()
        self.api_key_entry.setFixedWidth(400)
        self.api_key_layout.addWidget(self.api_key_label)
        self.api_key_layout.addWidget(self.api_key_entry)
        self.api_key_layout.addStretch()
        self.layout.addLayout(self.api_key_layout)

        # Channel Handles Input
        self.channel_layout = QHBoxLayout()
        self.channel_label = QLabel("Channel Handles (one per line):")
        self.channel_text = QTextEdit()
        self.channel_text.setFixedHeight(100)
        self.channel_text.setFixedWidth(400)
        self.channel_layout.addWidget(self.channel_label)
        self.channel_layout.addWidget(self.channel_text)
        self.channel_layout.addStretch()
        self.layout.addLayout(self.channel_layout)

        # Run Button
        self.run_button = QPushButton("Run")
        self.run_button.clicked.connect(self.start_scraping)
        self.layout.addWidget(self.run_button)

        # Status Label
        self.status_label = QLabel("Status: Idle")
        self.layout.addWidget(self.status_label)

        # Channel Selection List
        self.channel_list_label = QLabel("Select Channels to Save:")
        self.layout.addWidget(self.channel_list_label)
        self.channel_list = QListWidget()
        self.channel_list.setFixedHeight(100)
        self.layout.addWidget(self.channel_list)

        # Tab Widget for Results
        self.tab_widget = QTabWidget()
        self.tables = {}  # Dictionary to store tables for each channel
        self.layout.addWidget(self.tab_widget)

        # Save Button
        self.save_button = QPushButton("Save Selected to CSV")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_to_csv)
        self.layout.addWidget(self.save_button)

        # Results Storage
        self.url_list = []

    def create_new_tab(self, channel_handle):
        # Create a new table for the channel
        table = QTableWidget()
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["Video Title", "Video URL"])
        table.setColumnWidth(0, 400)
        table.setColumnWidth(1, 300)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tables[channel_handle] = table
        self.tab_widget.addTab(table, channel_handle)

        # Add channel to the selection list
        item = QListWidgetItem(channel_handle)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked)  # Default to checked
        self.channel_list.addItem(item)

    def start_scraping(self):
        self.tab_widget.clear()  # Clear existing tabs
        self.tables.clear()  # Clear table dictionary
        self.channel_list.clear()  # Clear channel selection list
        self.url_list = []
        self.run_button.setEnabled(False)
        self.save_button.setEnabled(False)

        api_key = self.api_key_entry.text().strip()
        channel_handles = [h.strip() for h in self.channel_text.toPlainText().splitlines() if h.strip()]

        self.scraper_thread = ScraperThread(api_key, channel_handles)
        self.scraper_thread.status_update.connect(self.update_status)
        self.scraper_thread.add_row.connect(self.add_table_row)
        self.scraper_thread.new_tab.connect(self.create_new_tab)
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
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(title))
        table.setItem(row, 1, QTableWidgetItem(url))
        self.url_list.append([channel_handle, title, url])

    def show_error(self, error_message):
        QMessageBox.critical(self, "Error", error_message)
        self.status_label.setText("Status: Error")

    def scraping_finished(self):
        self.run_button.setEnabled(True)

    def save_to_csv(self):
        if not self.url_list:
            QMessageBox.warning(self, "Warning", "No data to save")
            return

        # Get selected channels from the list
        selected_channels = []
        for i in range(self.channel_list.count()):
            item = self.channel_list.item(i)
            if item.checkState() == Qt.Checked:
                selected_channels.append(item.text())

        if not selected_channels:
            QMessageBox.warning(self, "Warning", "No channels selected")
            return

        # Prompt user to select output directory
        output_dir = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if not output_dir:
            return

        # Group results by channel handle and save selected channels
        channel_data = {}
        for channel_handle, title, url in self.url_list:
            if channel_handle in selected_channels:
                if channel_handle not in channel_data:
                    channel_data[channel_handle] = []
                channel_data[channel_handle].append([title, url])

        for channel_handle, data in channel_data.items():
            # Sanitize channel handle for filename
            safe_handle = "".join(c for c in channel_handle if c.isalnum() or c in ('-', '_')).rstrip()
            file_path = os.path.join(output_dir, f"{safe_handle}_output.csv")
            df = pd.DataFrame(data, columns=['Title', 'Url'])
            df.to_csv(file_path, index=True)
            self.status_label.setText(f"Status: Saved {file_path}")

        QMessageBox.information(self, "Success", f"Selected channels saved to individual CSV files in {output_dir}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = YouTubeScraperWindow()
    window.show()
    sys.exit(app.exec_())