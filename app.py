import os
import time
import sqlite3
import fitz  # PyMuPDF for PDF merging
import pandas as pd
from PIL import Image
from flask import (
    Flask, request, send_file, jsonify, flash,
    redirect, url_for, render_template, session, g
)
from flask_login import (
    LoginManager, UserMixin, login_user, login_required, logout_user, current_user
)
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Length
from werkzeug.utils import secure_filename
from io import BytesIO
from docx import Document
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supersecretkey")
MERGED_FOLDER = "/tmp/merged"
os.makedirs(MERGED_FOLDER, exist_ok=True)  # Ensure merged directory exists

# Database configuration
DATABASE = os.path.join(app.root_path, 'files.db')

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

def init_db():
    with app.app_context():
        db = get_db()
        # Create table for merged files
        db.execute("""
            CREATE TABLE IF NOT EXISTS merged_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_ip TEXT,
                timestamp TEXT,
                filename TEXT,
                team_id TEXT,
                salesforce_ticket TEXT,
                download_count INTEGER DEFAULT 0
            )
        """)
        # Create table for download logs
        db.execute("""
            CREATE TABLE IF NOT EXISTS download_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                merged_file_id INTEGER,
                downloader_ip TEXT,
                timestamp TEXT,
                FOREIGN KEY (merged_file_id) REFERENCES merged_files(id)
            )
        """)
        db.commit()

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

# Initialize the database if not exists
init_db()

# User authentication
login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "password")
USER_CREDENTIALS = {ADMIN_USERNAME: ADMIN_PASSWORD}

class User(UserMixin):
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    return User(user_id) if user_id in USER_CREDENTIALS else None

# Flask-WTF login form
class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(min=4, max=25)])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=4, max=25)])
    submit = SubmitField("Login")

ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'csv', 'docx'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def convert_to_pdf(file_obj):
    """Convert an uploaded file to PDF in memory and return a BytesIO."""
    ext = file_obj.filename.rsplit('.', 1)[1].lower()
    pdf_bytes = BytesIO()
    file_obj.seek(0)
    if ext == "pdf":
        pdf_bytes.write(file_obj.read())
    elif ext in ["png", "jpg", "jpeg"]:
        image = Image.open(file_obj)
        image.convert("RGB").save(pdf_bytes, format="PDF")
    elif ext == "csv":
        df = pd.read_csv(file_obj)
        pdf_bytes.write(df.to_string().encode("utf-8"))
    elif ext == "docx":
        doc = Document(file_obj)
        for para in doc.paragraphs:
            pdf_bytes.write((para.text + "\n").encode("utf-8"))
    pdf_bytes.seek(0)
    return pdf_bytes

def merge_pdfs(pdf_files, team_id):
    """Merge a list of in-memory PDF BytesIO objects and save to disk using team_id in filename."""
    date_str = time.strftime("%Y-%m-%d")
    filename = f"User {team_id} - Evidence {date_str}.pdf"
    merged_pdf_path = os.path.join(MERGED_FOLDER, filename)
    
    pdf_writer = fitz.open()
    for pdf_file in pdf_files:
        pdf_file.seek(0)
        pdf_reader = fitz.open(stream=pdf_file.read(), filetype="pdf")
        pdf_writer.insert_pdf(pdf_reader)
    pdf_writer.save(merged_pdf_path)
    return merged_pdf_path, filename

@app.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        if form.username.data in USER_CREDENTIALS and form.password.data == USER_CREDENTIALS[form.username.data]:
            user = User(form.username.data)
            login_user(user)
            session.pop('_flashes', None)  # Clear any previous flash messages
            return redirect(url_for("index"))
        flash("Invalid username or password", "danger")
    return render_template("login.html", form=form)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    db = get_db()
    if request.method == "POST":
        # Retrieve Dispute Ticket Details first
        team_id = request.form.get("team_id", "").strip()
        salesforce_ticket = request.form.get("salesforce_ticket", "").strip()
        if not team_id.isdigit() or not salesforce_ticket.isdigit():
            flash("Please provide valid numeric values for Team ID and Salesforce Ticket Number.", "danger")
            return redirect(url_for("index"))
        
        files = request.files.getlist("files")
        if not files:
            flash("Please select at least one file to upload.", "danger")
            return redirect(url_for("index"))
        
        pdf_bytes_list = []
        for file in files:
            if file and allowed_file(file.filename):
                pdf_bytes = convert_to_pdf(file)
                if pdf_bytes:
                    pdf_bytes_list.append(pdf_bytes)
        if not pdf_bytes_list:
            flash("No valid files to process!", "danger")
            return redirect(url_for("index"))
        # Merge PDFs using the team_id for naming
        merged_pdf_path, merged_filename = merge_pdfs(pdf_bytes_list, team_id)
        creator_ip = request.remote_addr
        timestamp_now = time.strftime("%Y-%m-%d %H:%M:%S")
        # Insert record into the database
        db.execute("""
            INSERT INTO merged_files (creator_ip, timestamp, filename, team_id, salesforce_ticket, download_count)
            VALUES (?, ?, ?, ?, ?, 0)
        """, (creator_ip, timestamp_now, merged_filename, team_id, salesforce_ticket))
        db.commit()
        # Instead of redirecting, return a page that auto-triggers the download
        return render_template("download_trigger.html", download_url=url_for("download", filename=merged_filename))
    
    # Pagination: 25 files per page
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    per_page = 25
    offset = (page - 1) * per_page
    cur = db.execute("SELECT * FROM merged_files ORDER BY id DESC LIMIT ? OFFSET ?", (per_page, offset))
    merged_files = cur.fetchall()
    
    files_list = []
    for file in merged_files:
        cur_logs = db.execute("SELECT * FROM download_logs WHERE merged_file_id = ? ORDER BY id DESC", (file["id"],))
        download_logs = cur_logs.fetchall()
        files_list.append({
            "id": file["id"],
            "creator_ip": file["creator_ip"],
            "timestamp": file["timestamp"],
            "filename": file["filename"],
            "team_id": file["team_id"],
            "salesforce_ticket": file["salesforce_ticket"],
            "download_count": file["download_count"],
            "download_logs": download_logs
        })
    
    total_files = db.execute("SELECT COUNT(*) as count FROM merged_files").fetchone()["count"]
    total_pages = (total_files + per_page - 1) // per_page

    # Render the default main page.
    return render_template("index.html", merged_files=files_list, page=page, total_pages=total_pages)

@app.route("/download/<filename>")
@login_required
def download(filename):
    file_path = os.path.join(MERGED_FOLDER, filename)
    if os.path.exists(file_path):
        db = get_db()
        cur = db.execute("SELECT id, download_count FROM merged_files WHERE filename = ?", (filename,))
        file_row = cur.fetchone()
        if file_row:
            new_count = file_row["download_count"] + 1
            db.execute("UPDATE merged_files SET download_count = ? WHERE id = ?", (new_count, file_row["id"]))
            db.execute("""
                INSERT INTO download_logs (merged_file_id, downloader_ip, timestamp)
                VALUES (?, ?, ?)
            """, (file_row["id"], request.remote_addr, time.strftime("%Y-%m-%d %H:%M:%S")))
            db.commit()
        return send_file(file_path, as_attachment=True)
    flash("File not found!", "danger")
    return redirect(url_for("index"))

@app.route("/cleanup", methods=["POST"])
def cleanup():
    now = time.time()
    for file in os.listdir(MERGED_FOLDER):
        file_path = os.path.join(MERGED_FOLDER, file)
        if os.stat(file_path).st_mtime < now - (30 * 86400):
            os.remove(file_path)
    return jsonify({"status": "Cleanup completed"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)