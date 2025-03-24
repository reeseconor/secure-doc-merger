import os
import time
import fitz  # PyMuPDF for PDFs
import pandas as pd
from PIL import Image
from flask import Flask, request, send_file, jsonify, flash, redirect, url_for, render_template
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user
from werkzeug.utils import secure_filename
from io import BytesIO
from docx import Document
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Length
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supersecretkey")

MERGED_FOLDER = "/tmp/merged"
os.makedirs(MERGED_FOLDER, exist_ok=True)  # Ensure merged directory exists

# User authentication
login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "password")

USER_CREDENTIALS = {
    ADMIN_USERNAME: ADMIN_PASSWORD
}

class User(UserMixin):
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    return User(user_id) if user_id in USER_CREDENTIALS else None

# Flask WTForms for login
class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(min=4, max=25)])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=4, max=25)])
    submit = SubmitField("Login")

ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'csv', 'docx'}

def allowed_file(filename):
    """ Check if the uploaded file type is allowed. """
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def convert_to_pdf(file):
    """ Converts uploaded file to a PDF format and returns it as a BytesIO object. """
    ext = file.filename.rsplit('.', 1)[1].lower()
    pdf_bytes = BytesIO()

    if ext == "pdf":
        pdf_bytes.write(file.read())  # Read file into memory
    elif ext in ["png", "jpg", "jpeg"]:
        image = Image.open(file)
        image.convert("RGB").save(pdf_bytes, format="PDF")
    elif ext == "csv":
        df = pd.read_csv(file)
        df.to_string(pdf_bytes)
    elif ext == "docx":
        doc = Document(file)
        for para in doc.paragraphs:
            pdf_bytes.write((para.text + "\n").encode("utf-8"))
    else:
        return None

    pdf_bytes.seek(0)  # Reset file pointer
    return pdf_bytes

def merge_pdfs(pdf_files):
    """ Merges multiple PDFs into one and saves it. Returns the merged file path. """
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    merged_pdf_path = os.path.join(MERGED_FOLDER, f"Merged_File_{timestamp}.pdf")

    pdf_writer = fitz.open()
    for pdf_file in pdf_files:
        pdf_reader = fitz.open(stream=pdf_file.read(), filetype="pdf")
        pdf_writer.insert_pdf(pdf_reader)

    pdf_writer.save(merged_pdf_path)
    return merged_pdf_path

@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        files = request.files.getlist("files")
        if not files:
            flash("No files provided!", "danger")
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

        merged_pdf_path = merge_pdfs(pdf_bytes_list)
        flash("Files merged successfully!", "success")
        return redirect(url_for("download", filename=os.path.basename(merged_pdf_path)))

    return render_template("index.html")

@app.route("/download/<filename>")
@login_required
def download(filename):
    file_path = os.path.join(MERGED_FOLDER, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    flash("File not found!", "danger")
    return redirect(url_for("index"))

@app.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        if form.username.data in USER_CREDENTIALS and form.password.data == USER_CREDENTIALS[form.username.data]:
            user = User(form.username.data)
            login_user(user)
            return redirect(url_for("index"))
        flash("Invalid username or password", "danger")
    return render_template("login.html", form=form)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/cleanup", methods=["POST"])
def cleanup():
    """ Deletes merged PDFs older than 30 days. """
    now = time.time()
    for file in os.listdir(MERGED_FOLDER):
        file_path = os.path.join(MERGED_FOLDER, file)
        if os.stat(file_path).st_mtime < now - (30 * 86400):  # 30 days
            os.remove(file_path)
    return jsonify({"status": "Cleanup completed"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
