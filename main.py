from flask import Flask, request, jsonify
import yt_dlp
import os
import pickle
import time
import json
import io
from dotenv import load_dotenv

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

app = Flask(__name__)

# Load environment variables
load_dotenv()

# Folder for downloaded videos
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Google Drive API scope
SCOPES = ["https://www.googleapis.com/auth/drive.file"]

def get_gdrive_service():
    """Authenticate and return Google Drive service instance"""
    creds = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as token:
            creds = pickle.load(token)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # Load credentials.json content from environment
            creds_json = os.getenv("GOOGLE_CREDENTIALS")
            if not creds_json:
                raise Exception("❌ GOOGLE_CREDENTIALS not found in environment variables")

            creds_dict = json.loads(creds_json)

            # Save creds_dict temporarily in memory
            flow = InstalledAppFlow.from_client_config(creds_dict, SCOPES)
            creds = flow.run_local_server(port=0)

        # Save token for reuse
        with open("token.pickle", "wb") as token:
            pickle.dump(creds, token)

    return build("drive", "v3", credentials=creds)

def get_or_create_folder(service, folder_name):
    """Get or create a Google Drive folder by name"""
    query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    # Create folder if not exists
    file_metadata = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
    folder = service.files().create(body=file_metadata, fields="id").execute()
    return folder.get("id")

@app.route("/download", methods=["POST"])
def video_to_drive():
    """Download YouTube video and upload to Google Drive"""
    data = request.get_json()
    url = data.get("url")
    print(f"Received URL: {url}")

    if not url:
        return jsonify({"error": "❌ YouTube URL required"}), 400

    outtmpl = os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")

    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": outtmpl,
    }

    try:
        # --- Download video ---
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if not filename.endswith(".mp4"):
                filename = filename.rsplit(".", 1)[0] + ".mp4"

        if not os.path.exists(filename):
            return jsonify({"error": "File not found after download"}), 500

        # --- Upload to Google Drive ---
        service = get_gdrive_service()
        folder_id = get_or_create_folder(service, "YouTubeSong")

        file_metadata = {
            "name": os.path.basename(filename),
            "parents": [folder_id]
        }

        media = MediaFileUpload(filename, mimetype="video/mp4", resumable=True)

        request_upload = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id, webViewLink"
        )

        response = None
        while response is None:
            status, response = request_upload.next_chunk()
            if status:
                print(f"⬆ Upload progress: {int(status.progress() * 100)}%")

        print("✅ Upload complete")

        # Close file handles
        try:
            if hasattr(media, "fd") and media.fd:
                media.fd.close()
            if hasattr(media, "_fd") and media._fd:
                media._fd.close()
        except Exception as e:
            print(f"⚠ Error closing file descriptor: {e}")

        # Make file public
        service.permissions().create(
            fileId=response["id"],
            body={"role": "reader", "type": "anyone"}
        ).execute()

        # --- Cleanup local file safely ---
        try:
            if os.path.exists(filename):
                os.remove(filename)
                print(f"🗑 Deleted local file: {filename}")
        except PermissionError:
            print("⚠ File was locked, retrying cleanup...")
            time.sleep(2)
            os.remove(filename)

        return jsonify({
            "message": "✅ Video downloaded & uploaded to Google Drive",
            "file_id": response.get("id"),
            "file_name": os.path.basename(filename),
            "public_link": response.get("webViewLink")
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/")
def home():
    return jsonify({"message": "🎬 YouTube → Google Drive API running"})

if __name__ == "__main__":
    app.run(port=5000, debug=True)
